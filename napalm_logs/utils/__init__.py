# -*- coding: utf-8 -*-
'''
napalm-logs utilities
'''
from __future__ import absolute_import
from __future__ import unicode_literals

# Import pythond stdlib
import re
import ssl
import copy
import time
import socket
import logging
import threading
import collections
from datetime import datetime

# Import python stdlib
import umsgpack
import nacl.secret
import nacl.signing
import nacl.encoding
from nacl.exceptions import CryptoError
from nacl.exceptions import BadSignatureError

# Import napalm-logs pkgs
import napalm_logs.config as defaults
from napalm_logs.exceptions import ClientConnectException
from napalm_logs.exceptions import CryptoException
from napalm_logs.exceptions import BadSignatureException

log = logging.getLogger(__name__)


class ClientAuth:
    '''
    Client auth class.
    '''
    def __init__(self,
                 certificate,
                 address=defaults.AUTH_ADDRESS,
                 port=defaults.AUTH_PORT,
                 timeout=defaults.AUTH_TIMEOUT,
                 max_try=defaults.AUTH_MAX_TRY):
        self.certificate = certificate
        self.address = address
        self.port = port
        self.timeout = timeout
        self.max_try = max_try
        self.try_id = 0
        self.priv_key = None
        self.verify_key = None
        self.ssl_skt = None
        self.__up = True
        self.authenticate()
        self._start_keep_alive()

    def _start_keep_alive(self):
        '''
        Start the keep alive thread as a daemon
        '''
        keep_alive_thread = threading.Thread(target=self.keep_alive)
        keep_alive_thread.daemon = True
        keep_alive_thread.start()

    def keep_alive(self):
        '''
        Send a keep alive request periodically to make sure that the server
        is still alive. If not then try to reconnect.
        '''
        self.ssl_skt.settimeout(defaults.AUTH_KEEP_ALIVE_INTERVAL)
        while self.__up:
            self.ssl_skt.send(defaults.AUTH_KEEP_ALIVE)
            msg = self.ssl_skt.recv(len(defaults.AUTH_KEEP_ALIVE_ACK))
            if msg != defaults.AUTH_KEEP_ALIVE_ACK:
                self.ssl_skt.close()
                self.reconnect()
            time.sleep(defaults.AUTH_KEEP_ALIVE_INTERVAL)

    def reconnect(self):
        '''
        Try to reconnect and re-authenticate with the server.
        '''
        while self.__up:
            try:
                self.authenticate()
            except socket.error:
                time.sleep(1)
            else:
                return

    def authenticate(self):
        '''
        Authenticate the client and return the private
        and signature keys.

        Establish a connection through a secured socket,
        then do the handshake using the napalm-logs
        auth algorithm.
        '''
        if ':' in self.address:
            skt_ver = socket.AF_INET6
        else:
            skt_ver = socket.AF_INET
        skt = socket.socket(skt_ver, socket.SOCK_STREAM)
        self.ssl_skt = ssl.wrap_socket(skt,
                                       ca_certs=self.certificate,
                                       cert_reqs=ssl.CERT_REQUIRED)
        try:
            self.ssl_skt.connect((self.address, self.port))
        except socket.error as err:
            self.try_id += 1
            if self.try_id < self.max_try:
                time.sleep(self.timeout)
                self.authenticate()
            raise ClientConnectException(err)

        # Explicit INIT
        self.ssl_skt.write(defaults.MAGIC_REQ)
        # Receive the private key
        private_key = self.ssl_skt.recv(defaults.BUFFER_SIZE)
        # Send back explicit ACK
        self.ssl_skt.write(defaults.MAGIC_ACK)
        # Read the hex of the verification key
        verify_key_hex = self.ssl_skt.recv(defaults.BUFFER_SIZE)
        # Send back explicit ACK
        self.ssl_skt.write(defaults.MAGIC_ACK)
        self.priv_key = nacl.secret.SecretBox(private_key)
        self.verify_key = nacl.signing.VerifyKey(verify_key_hex, encoder=nacl.encoding.HexEncoder)

    def decrypt(self, binary):
        '''
        Decrypt and unpack the original OpenConfig object,
        serialized using MessagePack.
        Raise BadSignatureException when the signature
        was forged or corrupted.
        '''
        try:
            encrypted = self.verify_key.verify(binary)
        except BadSignatureError:
            log.error('Signature was forged or corrupt', exc_info=True)
            raise BadSignatureException('Signature was forged or corrupt')
        try:
            packed = self.priv_key.decrypt(encrypted)
        except CryptoError:
            log.error('Unable to decrypt', exc_info=True)
            raise CryptoException('Unable to decrypt')
        return umsgpack.unpackb(packed)

    def stop(self):
        '''
        Stop the client.
        '''
        self.__up = False


def unserialize(binary):
    '''
    Unpack the original OpenConfig object,
    serialized using MessagePack.
    This is to be used when disable_security is set.
    '''
    return umsgpack.unpackb(binary)


def extract(rgx, msg, mapping, time_format=None):
    ret = {}
    log.debug('Matching regex "%s" on "%s"', rgx, msg)
    matched = re.search(rgx, msg, re.I)
    if not matched:
        log.info('The regex didnt match')
        return None
    else:
        group_index = 0
        for group_value in matched.groups():
            group_name = mapping.keys()[group_index]
            ret[group_name] = group_value
            group_index += 1
        log.debug('Regex matched')
        log.debug(ret)
    if time_format:
        try:
            ret['timestamp'] = int(datetime.strptime(time_format[0].format(**ret), time_format[1]).strftime('%s'))
        except ValueError as error:
            log.error('Unable to convert date and time into a timestamp: %s', error)
    return ret


def setval(key, val, dict_=None, delim=defaults.DEFAULT_DELIM):
    '''
    Set a value under the dictionary hierarchy identified
    under the key. The target 'foo/bar/baz' returns the
    dictionary hierarchy {'foo': {'bar': {'baz': {}}}}.

    .. note::

        Currently this doesn't work with integers, i.e.
        cannot build lists dynamically.
        TODO
    '''
    if not dict_:
        dict_ = {}
    prev_hier = dict_
    dict_hier = key.split(delim)
    for each in dict_hier[:-1]:
        try:
            idx = int(each)  # noqa
        except ValueError:
            # not int
            if each not in prev_hier:
                prev_hier[each] = {}
            prev_hier = prev_hier[each]
        else:
            prev_hier[each] = [{}]
            prev_hier = prev_hier[each]
    prev_hier[dict_hier[-1]] = val
    return dict_


def traverse(data, key, delim=defaults.DEFAULT_DELIM):
    '''
    Traverse a dict or list using a slash delimiter target string.
    The target 'foo/bar/0' will return data['foo']['bar'][0] if
    this value exists, otherwise will return empty dict.
    Return None when not found.
    This can be used to verify if a certain key exists under
    dictionary hierarchy.
    '''
    for each in key.split(delim):
        if isinstance(data, list):
            try:
                idx = int(each)
            except ValueError:
                embed_match = False
                # Index was not numeric, lets look at any embedded dicts
                for embedded in (x for x in data if isinstance(x, dict)):
                    try:
                        data = embedded[each]
                        embed_match = True
                        break
                    except KeyError:
                        pass
                if not embed_match:
                    # No embedded dicts matched
                    return None
            else:
                try:
                    data = data[idx]
                except IndexError:
                    return None
        else:
            try:
                data = data[each]
            except (KeyError, TypeError):
                return None
    return data


def dictupdate(dest, upd):
    '''
    Recursive version of the default dict.update
    Merges upd recursively into dest.
    '''
    recursive_update = True
    if (not isinstance(dest, collections.Mapping)) \
            or (not isinstance(upd, collections.Mapping)):
        raise TypeError('Cannot update using non-dict types in dictupdate.update()')
    updkeys = list(upd.keys())
    if not set(list(dest.keys())) & set(updkeys):
        recursive_update = False
    if recursive_update:
        for key in updkeys:
            val = upd[key]
            try:
                dest_subkey = dest.get(key, None)
            except AttributeError:
                dest_subkey = None
            if isinstance(dest_subkey, collections.Mapping) \
                    and isinstance(val, collections.Mapping):
                ret = dictupdate(dest_subkey, val)
                dest[key] = ret
            elif isinstance(dest_subkey, list) \
                    and isinstance(val, list):
                merged = copy.deepcopy(dest_subkey)
                merged.extend([x for x in val if x not in merged])
                dest[key] = merged
            else:
                dest[key] = upd[key]
        return dest
    else:
        try:
            for k in upd:
                dest[k] = upd[k]
        except AttributeError:
            # this mapping is not a dict
            for k in upd:
                dest[k] = upd[k]
        return dest
