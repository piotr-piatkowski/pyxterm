#!/usr/bin/env python
#
#  BSD License
#
#  Copyright (c) 2014, Ramalingam Saravanan <sarava@sarava.net>
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice, this
#     list of conditions and the following disclaimer.
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#  ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
#  ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from __future__ import absolute_import, print_function, with_statement

# Python3-friendly imports
try:
    from urllib.parse import urlparse, parse_qs, urlencode
except ImportError:
    from urlparse import urlparse, parse_qs
    from urllib import urlencode

import base64
import cgi
import collections
import logging
import os
import ssl
import sys
import threading
import time
import uuid

try:
    import ujson as json
except ImportError:
    import json

from pyxterm import pyxshell

import tornado.auth
import tornado.httpserver
import tornado.ioloop
import tornado.options
import tornado.web
import tornado.websocket

from redis_cache import get_redis_connection

try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

class TermSocket(tornado.websocket.WebSocketHandler):
    _all_term_sockets = {}
    _all_term_paths = collections.defaultdict(set)
    _term_counter = 0

    def get_term_command(self, key):
        rconn = get_redis_connection()
        term_info = rconn.get("term:{}".format(key))
        return term_info

    def open(self, key):
        logging.info("TermSocket.open:")

        self.term_path = key

        cmd = self.get_term_command(key)
        if cmd is None:
            logging.error("TermSocket.unknown terminal key: %s", key)
            self.term_remote_call("errmsg",
                "unknown terminal key: {}".format(key))
            self.close()
            return

        self.add_termsocket()

        command = ['/bin/bash', '-c', cmd]
        Term_manager.terminal(self.term_path, shell_command=command)

        self.term_remote_call("setup", {"client_id": self.term_client_id,
                                        "term_path": self.term_path})
        logging.info("TermSocket.open: Opened %s", self.term_path)

    @classmethod
    def get_path_termsockets(cls, path):
        return cls._all_term_paths.get(path, set())

    @classmethod
    def get_termsocket(cls, client_id):
        return cls._all_term_sockets.get(client_id)

    def add_termsocket(self):
        self._term_counter += 1
        self.term_client_id = str(self._term_counter)

        self._all_term_sockets[self.term_client_id] = self
        self._all_term_paths[self.term_path].add(self.term_client_id)
        return self.term_client_id

    def on_close(self):
        logging.info("TermSocket.on_close: Closing %s", self.term_path)
        self._all_term_sockets.pop(self.term_client_id, None)
        if self.term_path in self._all_term_paths:
            self._all_term_paths[self.term_path].discard(self.term_client_id)

    @classmethod
    def term_remote_callback(cls, term_path, client_id, method, *args):
        client_ids = [client_id] if client_id else cls.get_path_termsockets(term_path)
        try:
            json_msg = json.dumps([method, args])
            for client_id in client_ids:
                termsocket = cls.get_termsocket(client_id)
                if termsocket:
                    termsocket.term_write(json_msg)
        except Exception as excp:
            logging.error("term_remote_callback: ERROR %s", excp)

    def term_remote_call(self, method, *args):
        if __debug__:
            logging.debug("term_remote_call: %s", method)
        try:
            # Text message
            json_msg = json.dumps([method, args])
            self.term_write(json_msg)
        except Exception as excp:
            logging.error("term_remote_call: ERROR %s", excp)

    def term_write(self, data, binary=False):
        try:
            self.write_message(data, binary=binary)
        except Exception as excp:
            logging.error("term_write: ERROR %s", excp)
            closed_excp = getattr(tornado.websocket, "WebSocketClosedError",
                None)
            if not closed_excp or not isinstance(excp, closed_excp):
                import traceback
                logging.info("Error in websocket: %s\n%s",
                        excp, traceback.format_exc())
            try:
                # Close websocket on write error
                self.close()
            except Exception:
                pass

    def on_message(self, message):
        if not self.term_path:
            return

        if isinstance(message, bytes):
            # Binary message with UTF-16 JSON prefix
            enc_delim = "\n\n".encode("utf-16")
            offset = message.find(enc_delim)
            if offset < 0:
                raise Exception("Delimiter not found in binary message")
            command = json.loads(message[:offset]).decode("utf-16")
            content = message[offset+len(enc_delim):]
        else:
            command = json.loads(message if isinstance(message,str)
                else message.encode("UTF-8", "replace"))
            content = None

        kill_term = False
        try:
            send_cmd = True
            if command[0] == "kill_term":
                kill_term = True
            elif command[0] == "errmsg":
                logging.error("Terminal %s: %s", self.term_path, command[1])
                send_cmd = False

            if send_cmd:
                if command[0] == "stdin":
                    text = command[1].replace("\r\n","\n").replace("\r","\n")
                    Term_manager.term_write(self.term_path, text)
                else:
                    Term_manager.remote_term_call(self.term_path, *command)
                if kill_term:
                    self.kill_remote()

        except Exception as excp:
            logging.error("TermSocket.on_message: ERROR %s", excp)
            self.term_remote_call("errmsg", str(excp))
            return

    def kill_remote(self):
        for client_id in self.get_path_termsockets(self.term_path):
            tsocket = self.get_termsocket(client_id)
            if tsocket:
                tsocket.on_close()
                tsocket.close()
        try:
            Term_manager.kill_term(self.term_path)
        except Exception:
            pass

