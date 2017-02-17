from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import hashlib
import random
import select
import socket
import struct
import sys
import time
import traceback
import warnings

from itertools import chain
from logging import (DEBUG, INFO)

from async_promises import Promise
from typing import (cast, Any, Callable, Dict, Iterable, Iterator, Set, Tuple,
                    Union)

try:
    from .cbase import protocol as Protocol
except:
    from .base import Protocol

from .base import (flags, compression, to_base_58, from_base_58,
                   base_connection, message, base_daemon, base_socket,
                   InternalMessage, compression, MsgPackable)
from .mesh import (mesh_connection, mesh_daemon, mesh_socket)
from .utils import (inherit_doc, getUTC, get_socket, intersect, awaiting_value,
                    most_common, log_entry, sanitize_packet)

max_outgoing = 4
default_protocol = Protocol('chord', "Plaintext")  # SSL")
hashes = [b'sha1', b'sha224', b'sha256', b'sha384', b'sha512']

if sys.version_info >= (3, ):
    xrange = range


def distance(a, b, limit=None):
    #type: (int, int, Union[None, int]) -> int
    """This is a clockwise ring distance function. It depends on a globally
    defined k, the key size. The largest possible node id is limit (or
    ``2**384``).
    """
    return (b - a) % (
        limit or
        0x1000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
    )


def get_hashes(key):
    #type: (bytes) -> Tuple[int, int, int, int, int]
    """Returns the (adjusted) hashes for a given key. This is in the order of:

    - SHA1 (shifted 224 bits left)
    - SHA224 (shifted 160 bits left)
    - SHA256 (shifted 128 bits left)
    - SHA384 (unadjusted)
    - SHA512 (unadjusted)

    The adjustment is made to allow better load balancing between nodes, which
    assign responisbility for a value based on their SHA384-assigned ID.
    """
    return (
        int(hashlib.sha1(key).hexdigest(), 16) << 224,  # 384 - 160
        int(hashlib.sha224(key).hexdigest(), 16) << 160,  # 384 - 224
        int(hashlib.sha256(key).hexdigest(), 16) << 128,  # 384 - 256
        int(hashlib.sha384(key).hexdigest(), 16),
        int(hashlib.sha512(key).hexdigest(), 16))


class chord_connection(mesh_connection):
    """The class for chord connection abstraction. This inherits from
    :py:class:`py2p.mesh.mesh_connection`

    .. inheritance-diagram:: py2p.chord.chord_connection
    """
    __slots__ = mesh_connection.__slots__ + ('leeching', '__id_10')

    @log_entry('py2p.chord.chord_connection.__init__', DEBUG)
    @inherit_doc(mesh_connection.__init__)
    def __init__(self, *args, **kwargs):
        #type: (Any, *Any, **Any) -> None
        super(chord_connection, self).__init__(*args, **kwargs)
        self.leeching = True
        self.__id_10 = -1

    @property
    def id_10(self):
        #type: (chord_connection) -> int
        """Returns the nodes ID as an integer"""
        if self.__id_10 == -1:
            self.__id_10 = from_base_58(self.id)
        return self.__id_10


class chord_daemon(mesh_daemon):
    """The class for chord daemon.
    This inherits from :py:class:`py2p.mesh.mesh_daemon`

    .. inheritance-diagram:: py2p.chord.chord_daemon
    """

    @log_entry('py2p.chord.chord_daemon.__init__', DEBUG)
    @inherit_doc(mesh_daemon.__init__)
    def __init__(self, *args, **kwargs):
        #type: (Any, *Any, **Any) -> None
        super(chord_daemon, self).__init__(*args, **kwargs)
        self.conn_type = chord_connection

    @inherit_doc(mesh_daemon.handle_accept)
    def handle_accept(self):
        #type: (chord_daemon) -> chord_connection
        handler = super(chord_daemon, self).handle_accept()
        self.server._send_meta(handler)
        return cast(chord_connection, handler)


class chord_socket(mesh_socket):
    """
    The class for chord socket abstraction. This inherits from :py:class:`py2p.mesh.mesh_socket`

    .. inheritance-diagram:: py2p.chord.chord_socket

    Added Events:

    .. py:function:: py2p.chord.chord_socket Event 'add'(conn, key)

        This event is triggered when a key is added to the distributed
        dictionary. Because value information is not transmitted in this
        message, you must specifically request it.

        :param py2p.chord.chord_socket conn: A reference to this abstract socket
        :param bytes key: The key which has a new value

    .. py:function:: on('delete', func)

        This event is triggered when a key is deleted from your distributed
        dictionary.

        :param py2p.chord.chord_socket conn: A reference to this abstract socket
        :param bytes key: The key which has a new value"""
    __slots__ = mesh_socket.__slots__ + ('id_10', 'data', '__keys', 'leeching')

    @log_entry('py2p.chord.chord_socket.__init__', DEBUG)
    @inherit_doc(mesh_socket.__init__)
    def __init__(
            self,  #type: Any
            addr,  #type: str
            port,  #type: int
            prot=default_protocol,  #type: Protocol
            out_addr=None,  #type: Union[None, Tuple[str, int]]
            debug_level=0  #type: int
    ):  #type: (...) -> None
        """Initialize a chord socket"""
        if not hasattr(self, 'daemon'):
            self.daemon = 'chord reserved'
        super(chord_socket, self).__init__(addr, port, prot, out_addr,
                                           debug_level)
        if self.daemon == 'chord reserved':
            self.daemon = chord_daemon(addr, port, self)
        self.id_10 = from_base_58(self.id)  #type: int
        self.data = dict((
            (method, {})
            for method in hashes))  #type: Dict[bytes, Dict[int, MsgPackable]]
        self.__keys = set()  #type: Set[bytes]
        self.leeching = True  #type: bool
        # self.register_handler(self._handle_peers)
        self.register_handler(self.__handle_meta)
        self.register_handler(self.__handle_key)
        self.register_handler(self.__handle_retrieve)
        self.register_handler(self.__handle_retrieved)
        self.register_handler(self.__handle_store)

    @property
    def addr(self):
        #type: (chord_socket) -> Tuple[str, int]
        """An alternate binding for ``self.out_addr``, in order to better handle self-references in pathfinding"""
        return self.out_addr

    @property
    def data_storing(self):
        #type: (chord_socket) -> Iterator[chord_connection]
        for _node in self.routing_table.values():
            node = cast(chord_connection, _node)
            if not node.leeching:
                yield node

    def disconnect_least_efficient(self):
        #type: (chord_socket) -> bool
        """Disconnects the node which provides the least value.

        This is determined by finding the node which is the closest to
        its neighbors, using the modulus distance metric

        Returns:
            A :py:class:`bool` that describes whether a node was disconnected
        """

        @inherit_doc(chord_connection.id_10)
        def get_id(o):
            #type: (chord_connection) -> int
            return o.id_10

        def smallest_gap(lst):
            #type: (Iterable[chord_connection]) -> chord_connection
            coll = sorted(lst, key=get_id)
            coll_len = len(coll)
            circular_triplets = (
                (coll[x], coll[(x + 1) % coll_len], coll[(x + 2) % coll_len])
                for x in range(coll_len))
            narrowest = None  #type: Union[None, chord_connection]
            gap = 2**384  #type: int
            for beg, mid, end in circular_triplets:
                if distance(beg.id_10, end.id_10) < gap and mid.outgoing:
                    gap = distance(beg.id_10, end.id_10)
                    narrowest = mid
            return narrowest

        relevant_nodes = (node for node in self.data_storing
                          if not node.leeching)
        to_kill = smallest_gap(relevant_nodes)
        if to_kill:
            self.disconnect(to_kill)
            return True
        return False

    def __handle_meta(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with chord specific metadata.
        Its primary job is:

        - set connection state

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.handshake and len(packets) == 2:
            new_meta = bool(int(packets[1]))
            conn = cast(chord_connection, handler)
            if new_meta != conn.leeching:
                self._send_meta(conn)
                conn.leeching = new_meta
                if not self.leeching and not conn.leeching:
                    conn.send(flags.whisper, flags.peers,
                              self._get_peer_list())
                    update = self.dump_data(conn.id_10, self.id_10)
                    for method, table in update.items():
                        for key, value in table.items():
                            self.__print__(method, key, value, level=5)
                            self.__store(method, key, value)
                if len(tuple(self.outgoing)) > max_outgoing:
                    self.disconnect_least_efficient()
            return True

    def __handle_key(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with new key entries. Its primary
        job is:

        - Ensure keylist syncronization

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.notify:
            if len(packets) == 3:
                if packets[1] in self.__keys:
                    self.__keys.remove(packets[1])
                    self.emit('delete', self, packets[1])
            else:
                self.__keys.add(packets[1])
                self.emit('add', self, packets[1])
            return True

    def _handle_peers(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with peer signals. Its primary jobs
        is to connect to the given peers, if this does not exceed
        :py:const:`py2p.chord.max_outgoing`

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.peers:
            new_peers = packets[1]

            def is_prev(id):
                #type: (Union[bytes, bytearray, str]) -> bool
                return distance(from_base_58(id), self.id_10) <= distance(
                    self.prev.id_10, self.id_10)

            def is_next(id):
                #type: (Union[bytes, bytearray, str]) -> bool
                return distance(self.id_10, from_base_58(id)) <= distance(
                    self.id_10, self.next.id_10)

            for addr, id in new_peers:
                if len(tuple(self.outgoing)) < max_outgoing or is_prev(
                        id) or is_next(id):
                    try:
                        self.__connect(addr[0], addr[1], id.encode())
                    except:  # pragma: no cover
                        self.__print__(
                            "Could not connect to %s because\n%s" %
                            (addr, traceback.format_exc()),
                            level=1)
                        continue
            return True

    def __handle_retrieved(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with response signals. Its two
        primary jobs are:

        - if it was your request, send the deferred message
        - if it was someone else's request, relay the information

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.retrieved:
            self.__print__(
                "Response received for request id %s" % packets[1], level=1)
            if self.requests.get((packets[1], packets[2])):
                value = cast(awaiting_value,
                             self.requests.get((packets[1], packets[2])))
                value.value = packets[3]
                if value.callback:
                    value.callback_method(packets[1], packets[2])
            return True

    def __handle_retrieve(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with data retrieval signals. Its two primary jobs are:

        - respond with data you possess
        - if you don't possess it, make a request with your closest peer to that key

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.retrieve:
            if packets[1] in hashes:
                val = self.__lookup(packets[1],
                                    from_base_58(packets[2]),
                                    cast(chord_connection, handler))
                if val.value not in {None, b''}:
                    self.__print__(val.value, level=1)
                    handler.send(flags.whisper, flags.retrieved, packets[1],
                                 packets[2], cast(MsgPackable, val.value))
                return True

    def __handle_store(self, msg, handler):
        #type: (chord_socket, message, base_connection) -> Union[bool, None]
        """This callback is used to deal with data storage signals. Its two primary jobs are:

        - store data in keys you're responsible for
        - if you aren't responsible, make a request with your closest peer to that key

        Args:
            msg:        A :py:class:`~py2p.base.message`
            handler:    A :py:class:`~py2p.chord.chord_connection`

        Returns:
            Either ``True`` or ``None``
        """
        packets = msg.packets
        if packets[0] == flags.store:
            method = packets[1]
            key = from_base_58(packets[2])
            self.__store(method, key, packets[3])
            return True

    def dump_data(self, start, end):
        #type: (chord_socket, int, int) -> Dict[bytes, Dict[int, MsgPackable]]
        """Args:
            start:  An :py:class:`int` which indicates the start of the desired key range.
                        ``0`` will get all data.
            end:    An :py:class:`int` which indicates the end of the desired key range.
                        ``None`` will get all data.

        Returns:
            A nested :py:class:`dict` containing your data from start to end
        """
        ret = dict((
            (method, {})
            for method in hashes))  #type: Dict[bytes, Dict[int, MsgPackable]]
        self.__print__("Entering dump_data", level=1)
        for method, table in self.data.items():
            for key, value in table.items():
                if distance(start, key) < distance(end, key):
                    self.__print__(method, key, level=6)
                    ret[method][key] = value
        return ret

    def __lookup(self, method, key, handler=None):
        #type: (chord_socket, bytes, int, chord_connection) -> awaiting_value
        """Looks up the value at a given hash function and key. This method
        deals with just *one* of the underlying hash tables.

        Args:
            method: The hash table that you wish to check. Must be a
                        :py:class:`str` or :py:class:`bytes`-like object
            key:    The key that you wish to check. Must be a :py:class:`int` or
                        :py:class:`long`

        Returns:
            The value at said key in an :py:class:`py2p.utils.awaiting_value`
            object, which either contains or will eventually contain its result
        """
        node = self  #type: Union[chord_socket, base_connection]
        if self.routing_table:
            node = self.find(key)
        elif self.awaiting_ids:
            node = random.choice(self.awaiting_ids)
        if node in (self, None):
            return awaiting_value(self.data[method].get(key, ''))
        else:
            node.send(flags.whisper, flags.retrieve, method, to_base_58(key))
            ret = awaiting_value()
            if handler:
                ret.callback = handler
            self.requests[method, to_base_58(key)] = ret
            return ret

    def __getitem(self, key, timeout=10):
        #type: (chord_socket, bytes, int) -> MsgPackable
        """Looks up the value at a given key.
        Under the covers, this actually checks five different hash tables, and
        returns the most common value given.

        Args:
            key:        The key that you wish to check. Must be a :py:class:`str` or
                            :py:class:`bytes`-like object
            timeout:    The longest you would like to await a value (default: 10s)

        Returns:
            The value at said key

        Raises:
            socket.timeout: If the request goes partly-unanswered for >=timeout seconds
            KeyError:       If the request is made for a key with no agreed-upon value

        Note:
            It's probably much better to use :py:func:`~py2p.chord.chord_socket.get`
        """
        key = sanitize_packet(key)
        self._logger.debug('Getting value of {}'.format(key))
        keys = get_hashes(key)
        vals = [self.__lookup(method, x) for method, x in zip(hashes, keys)]
        common, count = most_common(vals)
        iters = 0
        limit = timeout // 0.1
        fails = {None, b''}
        while (common in fails or count <= len(hashes) // 2) and iters < limit:
            time.sleep(0.1)
            iters += 1
            common, count = most_common(vals)
        if common not in fails and count > len(hashes) // 2:
            return common
        elif iters == limit:
            raise socket.timeout()
        raise KeyError(
            "This key does not have an agreed-upon value. "
            "values={}, count={}, majority={}, most common ={}".format(
                vals, count, len(hashes) // 2 + 1, common))

    def __getitem__(self, key):
        #type: (chord_socket, bytes) -> MsgPackable
        """Looks up the value at a given key.
        Under the covers, this actually checks five different hash tables, and
        returns the most common value given.

        Args:
            key:        The key that you wish to check. Must be a :py:class:`str` or
                            :py:class:`bytes`-like object

        Returns:
            The value at said key

        Raises:
            socket.timeout: If the request goes partly-unanswered for >=timeout seconds
            KeyError:       If the request is made for a key with no agreed-upon value

        Note:
            It's probably much better to use :py:func:`~py2p.chord.chord_socket.get`
        """
        return self.__getitem(key)

    def getSync(self, key, ifError=None, timeout=10):
        #type: (chord_socket, bytes, MsgPackable, int) -> MsgPackable
        """Looks up the value at a given key.
        Under the covers, this actually checks five different hash tables, and
        returns the most common value given.

        Args:
            key:     The key that you wish to check. Must be a :py:class:`str`
                        or :py:class:`bytes`-like object
            ifError: The value you wish to return on exception (default:
                        ``None``)
            timeout: The longest you would like to await a value (default: 10s)

        Returns:
            The value at said key, or the value at ifError if there's an
            :py:class:`Exception`

        Note:
            It's probably much better to use :py:func:`~py2p.chord.chord_socket.get`
        """
        try:
            self._logger.debug(
                'Getting value of {}, with fallback'.format(key, ifError))
            return self.__getitem(key, timeout=timeout)
        except (KeyError, socket.timeout):
            return ifError

    def get(self, key, ifError=None, timeout=10):
        #type: (chord_socket, bytes, MsgPackable, int) -> Promise
        """Looks up the value at a given key.
        Under the covers, this actually checks five different hash tables, and
        returns the most common value given.

        Args:
            key:     The key that you wish to check. Must be a :py:class:`str`
                        or :py:class:`bytes`-like object
            ifError: The value you wish to return on exception (default:
                        ``None``)
            timeout: The longest you would like to await a value (default: 10s)

        Returns:
            A :py:class:`~async_promises.Promise` of the value at said key, or
            the value at ifError if there's an :py:class:`Exception`
        """

        def resolver(resolve, reject):
            #type: (Callable, Callable) -> None
            resolve(self.getSync(key, ifError=ifError, timeout=timeout))

        self._logger.debug(
            'Getting Promise of {}, with fallback'.format(key, ifError))
        return Promise(resolver)

    def __store(self, method, key, value):
        #type: (chord_socket, bytes, int, MsgPackable) -> None
        """Updates the value at a given key. This method deals with just *one*
        of the underlying hash tables.

        Args:
            method: The hash table that you wish to check. Must be a
                        :py:class:`str` or :py:class:`bytes`-like object
            key:    The key that you wish to check. Must be a :py:class:`int` or
                        :py:class:`long`
            value:  The value you wish to put at this key. Must be a :py:class:`str`
                        or :py:class:`bytes`-like object
        """
        node = self.find(key)  #type: Union[chord_socket, base_connection]
        if self.leeching and node is self and len(self.awaiting_ids):
            node = random.choice(self.awaiting_ids)
        if node in (self, None):
            if value == b'':
                del self.data[method][key]
            else:
                self.data[method][key] = value
        else:
            node.send(flags.whisper, flags.store, method,
                      to_base_58(key), value)

    def __setitem__(self, key, value):
        #type: (chord_socket,  Union[bytes, bytearray, str], MsgPackable) -> None
        """Updates the value at a given key.
        Under the covers, this actually uses five different hash tables, and
        updates the value in all of them.

        Args:
            key:    The key that you wish to update. Must be a :py:class:`str` or
                        :py:class:`bytes`-like object
            value:  The value you wish to put at this key.

        Raises:

            TypeError: If your key is not :py:class:`bytes` -like OR if your
                        value is not serializable. This means your value must
                        be one of the following:

                        - :py:class:`bool`
                        - :py:class:`float`
                        - :py:class:`int` (if ``2**64 > x > -2**63``)
                        - :py:class:`str`
                        - :py:class:`bytes`
                        - :py:class:`unicode`
                        - :py:class:`tuple`
                        - :py:class:`list`
                        - :py:class:`dict` (if all keys are :py:class:`unicode`)
        """
        _key = sanitize_packet(key)
        self._logger.debug('Setting value of {} to {}'.format(_key, value))
        keys = get_hashes(_key)
        for method, x in zip(hashes, keys):
            self.__store(method, x, value)
        if _key not in self.__keys and value != b'':
            self.__keys.add(_key)
            self.send(_key, type=flags.notify)
        elif _key in self.__keys and value == b'':
            self.__keys.add(_key)
            self.send(_key, b'del', type=flags.notify)

    @inherit_doc(__setitem__)
    def set(self, key, value):
        #type: (chord_socket, Union[bytes, bytearray, str], MsgPackable) -> None
        self.__setitem__(key, value)

    def __delitem__(self, key):
        #type: (chord_socket, Union[bytes, bytearray, str]) -> None
        _key = sanitize_packet(key)
        if _key not in self.__keys:
            raise KeyError(_key)
        self.set(_key, b'')

    def update(self, update_dict):
        #type: (chord_socket, Dict[Union[bytes, bytearray, str], MsgPackable]) -> None
        """Equivalent to :py:meth:`dict.update`

        This calls :py:meth:`.chord_socket.store` for each key/value pair in the
        given dictionary.


        Args:
            update_dict: A :py:class:`dict`-like object to extract key/value pairs from.
                            Key and value be a :py:class:`str` or :py:class:`bytes`-like
                            object
        """
        for key, value in update_dict.items():
            self.__setitem__(key, value)

    def find(self, key):
        #type: (chord_socket, int) -> Union[chord_socket, chord_connection]
        """Finds the node which is responsible for a certain value. This does
        not necessarily mean that they are supposed to store that value, just
        that they are along your path to said node.

        Args:
            key:    The key that you wish to check. Must be a :py:class:`int` or
                        :py:class:`long`

        Returns: A :py:class:`~py2p.chord.chord_connection` or this socket
        """
        if not self.leeching:
            ret = self  #type: Union[chord_socket, chord_connection]
            gap = distance(self.id_10, key)
        else:
            ret = None
            gap = 2**384
        for handler in self.data_storing:
            dist = distance(handler.id_10, key)
            if dist < gap:
                ret = handler
                gap = dist
        return ret

    def find_prev(self, key):
        #type: (chord_socket, int) -> Union[chord_socket, chord_connection]
        """Finds the node which is farthest from a certain value. This is used
        to find a node's "predecessor"; the node it is supposed to delegate to
        in the event of a disconnections.

        Args:
            key:    The key that you wish to check. Must be a :py:class:`int` or
                        :py:class:`long`

        Returns: A :py:class:`~py2p.chord.chord_connection` or this socket
        """
        if not self.leeching:
            ret = self  #type: Union[chord_socket, chord_connection]
            gap = distance(key, self.id_10)
        else:
            ret = None
            gap = 2**384
        for handler in self.data_storing:
            dist = distance(key, handler.id_10)
            if dist < gap:
                ret = handler
                gap = dist
        return ret

    @property
    def next(self):
        #type: (chord_socket) -> Union[chord_socket, chord_connection]
        """The connection that is your nearest neighbor *ahead* on the
        hash table ring
        """
        return self.find(self.id_10 - 1)

    @property
    def prev(self):
        #type: (chord_socket) -> Union[chord_socket, chord_connection]
        """The connection that is your nearest neighbor *behind* on the
        hash table ring
        """
        return self.find_prev(self.id_10 + 1)

    def _send_meta(self, handler):
        #type: (chord_socket, chord_connection) -> None
        """Shortcut method for sending a chord-specific data to a given handler

        Args:
            handler: A :py:class:`~py2p.chord.chord_connection`
        """
        handler.send(flags.whisper, flags.handshake, str(int(self.leeching)))
        for key in self.__keys.copy():
            handler.send(flags.whisper, flags.notify, key)

    def __connect(self, addr, port, id=None):
        #type: (chord_socket, str, int, bytes) -> None
        """Private API method for connecting and handshaking

        Args:
            addr: the address you want to connect to/handshake
            port: the port you want to connect to/handshake
        """
        try:
            handler = self.connect(addr, port, id)
            if handler and not self.leeching:
                self._send_handshake(handler)
                self._send_meta(handler)
        except:
            pass

    def join(self):
        #type: (chord_socket) -> None
        """Tells the node to start seeding the chord table"""
        # for handler in self.awaiting_ids:
        self._logger.debug('Joining the network data store')
        self.leeching = False
        for handler in tuple(self.routing_table.values()) + tuple(
                self.awaiting_ids):
            self._send_handshake(cast(mesh_connection, handler))
            self._send_peers(handler)
            self._send_meta(cast(chord_connection, handler))

    def unjoin(self):
        #type: (chord_socket) -> None
        """Tells the node to stop seeding the chord table"""
        self._logger.debug('Unjoining the network data store')
        self.leeching = True
        for handler in tuple(self.routing_table.values()) + tuple(
                self.awaiting_ids):
            self._send_handshake(cast(mesh_connection, handler))
            self._send_peers(handler)
            self._send_meta(cast(chord_connection, handler))
        for method in self.data.keys():
            for key, value in self.data[method].items():
                self.__store(method, key, value)
            self.data[method].clear()

    def __del__(self):
        #type: (chord_socket) -> None
        self.unjoin()
        super(chord_socket, self).__del__()

    @inherit_doc(mesh_socket.connect)
    def connect(self, *args, **kwargs):
        #type: (chord_socket, *Any, **Any) -> Union[bool, None]
        if kwargs.get('conn_type'):
            return super(chord_socket, self).connect(*args, **kwargs)
        kwargs['conn_type'] = chord_connection
        return super(chord_socket, self).connect(*args, **kwargs)

    def keys(self):
        #type: (chord_socket) -> Iterator[bytes]
        """Returns an iterator of the underlying :py:class:`dict`'s keys"""
        self._logger.debug('Retrieving all keys')
        return (key for key in self.__keys if key in self.__keys)

    @inherit_doc(keys)
    def __iter__(self):
        #type: (chord_socket) -> Iterator[bytes]
        return self.keys()

    def values(self):
        #type: (chord_socket) -> Iterator[MsgPackable]
        """Returns:
            an iterator of the underlying :py:class:`dict`'s values
        Raises:
            KeyError:       If the key does not have a majority-recognized
                                value
            socket.timeout: See KeyError
        """
        self._logger.debug('Retrieving all values')
        keys = self.keys()
        nxt = self.get(next(keys))
        for key in keys:
            _nxt = self.get(key)
            if nxt.get():
                yield nxt.get()
            nxt = _nxt
        if nxt.get():
            yield nxt.get()

    def items(self):
        #type: (chord_socket) -> Iterator[Tuple[bytes, MsgPackable]]
        """Returns:
            an iterator of the underlying :py:class:`dict`'s items
        Raises:
            KeyError:       If the key does not have a majority-recognized
                                value
            socket.timeout: See KeyError
        """
        self._logger.debug('Retrieving all items')
        keys = self.keys()
        p_key = next(keys)
        nxt = self.get(p_key)
        for key in keys:
            _nxt = self.get(key)
            if nxt.get():
                yield (p_key, nxt.get())
            p_key = key
            nxt = _nxt
        if nxt.get():
            yield (p_key, nxt.get())

    def pop(self, key, *args):
        #type: (chord_socket, bytes, *Any) -> MsgPackable
        """Returns a value, with the side effect of deleting that association

        Args:
            Key:        The key you wish to look up. Must be a :py:class:`str`
                            or :py:class:`bytes`-like object
            ifError:    The value you wish to return on :py:class:`Exception`
                            (default: raise an :py:class:`Exception` )

        Returns:
            The value of the supplied key, or ``ifError``

        Raises:
            KeyError:       If the key does not have a majority-recognized
                                value
            socket.timeout: See KeyError
        """
        self._logger.debug('Popping key {}'.format(key))
        if len(args):
            ret = self.getSync(key, args[0])
            if ret != args[0]:
                del self[key]
        else:
            ret = self[key]
            del self[key]
        return ret

    def popitem(self):
        #type: (chord_socket) -> Tuple[bytes, MsgPackable]
        """Returns an association, with the side effect of deleting that
        association

        Returns:
            An arbitrary association

        Raises:
            KeyError:       If the key does not have a majority-recognized
                                value
            socket.timeout: See KeyError
        """
        self._logger.debug('Popping an item')
        key = next(self.keys())
        return (key, self.pop(key))

    def copy(self):
        #type: (chord_socket) -> Dict[bytes, MsgPackable]
        """Returns a :py:class:`dict` copy of this DHT

        .. warning::

            This is a *very* slow operation. It's a far better idea to use
            :py:meth:`~py2p.chord.chord_socket.items`, as this produces an
            iterator. That should even out lag times
        """
        self._logger.debug('Producing a dictionary copy')
        promises = [(key, self.get(key)) for key in self.keys()]
        return dict((key, p.get()) for key, p in promises)
