try:
    import rsa
except:
    raise Exception("You cannot use this without the rsa module. To install this, run 'pip install rsa'.")

from multiprocessing.pool import ThreadPool as Pool
import socket

key_request = "Requesting key".encode('utf-8')
end_of_message = '\x03\x04\x17\x04\x03'.encode('utf-8')
size_request = "Requesting key size".encode('utf-8')


class secureSocket(object):
    """An RSA encrypted and secured socket. Requires the rsa module"""
    def __init__(self, keysize=1024, suppress_warnings=False, *args, **kargs):
        if kargs.get('keysize'):
            keysize = kargs.pop('keysize')
        if kargs.get('suppress_warnings'):
            suppress_warnings = kargs.pop('suppress_warnings')
        if not suppress_warnings:
            if (keysize / 8) - 11 < len(end_of_message):
                raise ValueError("This is too small to be useful. You can't even send the end of message packet.")
            elif keysize > 4096:
                raise ValueError("This is too large to be practical. Receiving will be slow. Generating will be very slow.")
        self.sock = socket.socket(*args, **kargs)
        self.key_async = Pool().map_async(rsa.newkeys, [keysize])  # Gen in background to reduce block
        self.pub, self.priv = None, None    # Temporarily set to None so they can generate in background
        self.keysize = keysize
        self.msgsize = (keysize / 8) - 11
        self.key = None
        self.conn = None
        self.bound = False
        self.peer_keysize = None
        self.peer_msgsize = None

    def mapKey(self):
    """Deals with the asyncronous generation of keys"""
        if self.pub is None:
            self.pub, self.priv = self.key_async.get()[0]
            del self.key_async

    def bind(self, ip):
    """Wrapper for the socket's native bind method"""
        self.sock.bind(ip)
        self.bound = ip

    def listen(self, i):
    """Wrapper for the socket's native listen method"""
        self.sock.listen(i)

    def accept(self):
    """Accepts an incoming connection.
    Unlike a native socket, it doesn't return anything, it's handled intra-object."""
        if self.conn:
            self.conn.close()
        self.conn, self.addr = self.sock.accept()
        self.mapKey()
        self.sendKey()
        self.requestKey()

    def connect(self, ip):
    """Connects to another secureSocket"""
        if self.conn:
            self.conn.close()
        self.sock.connect(ip)
        self.conn = self.sock
        self.requestKey()
        self.mapKey()
        self.sendKey()

    def close(self):
    """Closes your connection to another socket, then cleans up metadata"""
        self.conn.close()
        self.conn = None
        self.key = None
        self.peer_keysize = None
        self.peer_msgsize = None
        if not self.bound:
            self.sock = socket.socket()

    def settimeout(self, i):
    """Wrapper for the socket's native settimeout method"""
        self.sock.settimeout(i)
        if self.conn:
            self.conn.settimeout(i)

    def send(self, msg):
    """Sends an encrypted copy of your message, and a signed+encrypted copy"""
        self.__send__(msg)
        self.__send__(self.sign(msg))

    def recv(self):
    """Receives and decrypts a message, then verifies it against the attached signature"""
        msg = self.__recv__()
        try:
            self.verify(msg, self.__recv__())
        except rsa.pkcs1.VerificationError:
            raise rsa.pkcs1.VerificationError("This message could not be verified. It's possible you are experiencing a man in the middle attack")
        return msg

    def sign(self, msg, hashop='SHA-256'):
    """Signs a message with a given hash (Default: SHA-256)"""
        return rsa.sign(msg, self.priv, hashop)

    def verify(self, msg, sig, key=None):
    """Verifies a message with a given key (Default: your peer's)"""
        if key is None:
            key = self.key
        return rsa.verify(msg, sig, key)

    def __send__(self, msg):
    """Base method for sending a message. Encrypts and sends"""
        if not isinstance(msg, type("a".encode('utf-8'))):
            msg = msg.encode('utf-8')
        x = 0
        while x < len(msg) - self.peer_msgsize:
            self.conn.sendall(rsa.encrypt(msg[x:x+self.peer_msgsize], self.key))
            x += self.peer_msgsize
        self.conn.sendall(rsa.encrypt(msg[x:], self.key))
        self.conn.sendall(rsa.encrypt(end_of_message, self.key))

    def __recv__(self):
    """Base method for receiving a message. Receives and decrypts."""
        received = "".encode('utf-8')
        packet = ""
        try:
            while True:
                packet = self.conn.recv(self.msgsize + 11)
                packet = rsa.decrypt(packet, self.priv)
                if packet == end_of_message:
                    return received
                received += packet
        except rsa.pkcs1.DecryptionError as error:
            print("Decryption error---Content: " + str(packet))
            return received

    def requestKey(self):
    """Requests your peer's key over plaintext"""
        while True:
            print("Requesting key size")
            self.conn.send(size_request)
            try:
                self.peer_keysize = int(self.conn.recv(16))
                self.peer_msgsize = (self.peer_keysize / 8) - 11
                print("Requesting key")
                self.conn.send(key_request)
                key = self.conn.recv(self.peer_keysize).split(",")
                self.key = rsa.PublicKey(int(key[0]), int(key[1]))
                print("Key received")
                break
            except EOFError:
                continue
    
    def sendKey(self):
    """Sends your key over plaintext"""
        if self.conn.recv(len(size_request)) != size_request:
            raise ValueError("Handshake has failed due to invalid request from peer")
        print("Sending key size")
        self.conn.sendall(str(self.keysize).encode("utf-8"))
        if self.conn.recv(len(key_request)) != key_request:
            raise ValueError("Handshake has failed due to invalid request from peer")
        print("Sending key")
        self.conn.sendall((str(self.pub.n) + "," + str(self.pub.e)).encode('utf-8'))
