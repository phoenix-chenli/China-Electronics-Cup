"""Offline acceptance guard: only loopback and Unix sockets may connect.

Mocked transports remain usable; an accidental real external request fails
before DNS or TCP. No personal connection settings are read by this helper.
"""
import atexit
import ipaddress
import json
import socket


def install():
    if getattr(socket, '_evidenceflow_offline', False): return
    socket._evidenceflow_offline=True
    original_connect,original_ex,original_dns=socket.socket.connect,socket.socket.connect_ex,socket.getaddrinfo
    counts={'loopback_connects':0,'external_blocked':0}
    def allowed(host):
        if host=='localhost':return True
        try:return ipaddress.ip_address(host).is_loopback
        except (ValueError,TypeError):return False
    def check(sock,address):
        # Windows has no AF_UNIX at all, so an unguarded comparison raised
        # AttributeError on every connection — including loopback — and took the
        # HTTP fixtures of 15 modules down in setUpClass before a single request.
        if getattr(socket,'AF_UNIX',None) is not None and sock.family==socket.AF_UNIX:return
        if not isinstance(address,tuple) or not allowed(address[0]):
            counts['external_blocked']+=1
            raise PermissionError('离线验收禁止连接外部网络；未发送模型请求')
        counts['loopback_connects']+=1
    def connect(sock,address):check(sock,address);return original_connect(sock,address)
    def connect_ex(sock,address):check(sock,address);return original_ex(sock,address)
    def dns(host,*args,**kwargs):
        if host is not None and not allowed(host):
            counts['external_blocked']+=1
            raise PermissionError('离线验收禁止外部DNS解析；未发送模型请求')
        return original_dns(host,*args,**kwargs)
    socket.socket.connect=connect;socket.socket.connect_ex=connect_ex;socket.getaddrinfo=dns
    atexit.register(lambda:print('OFFLINE_NETWORK '+json.dumps(counts),flush=True))
