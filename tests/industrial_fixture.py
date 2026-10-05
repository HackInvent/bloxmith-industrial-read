"""Disposable loopback Modbus and real asyncua servers; never contact equipment."""

import asyncio
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import ipaddress
import json
from pathlib import Path
import socket
import socketserver
import struct
import threading
from types import SimpleNamespace

REF="secret://workspace/industrial_fixture"
CLIENT_URI="urn:hackinvent:bloxsmith:industrial-read"


def take(sock,count):
    data=b""
    while len(data)<count:
        chunk=sock.recv(count-len(data))
        if not chunk:
            raise EOFError()
        data+=chunk
    return data


@contextmanager
def modbus_server():
    api=SimpleNamespace(requests=[],mode="ok",connections=0,closed=0,release=threading.Event(),values={10:1234,20:0x4148,21:0})
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            api.connections+=1;self.request.settimeout(3)
            try:
                while True:
                    tid,protocol,length,unit=struct.unpack(">HHHB",take(self.request,7))
                    assert protocol==0 and length==6 and unit==1
                    function,address,count=struct.unpack(">BHH",take(self.request,5))
                    api.requests.append((function,address,count))
                    assert function in (1,2,3,4),"Block sent a forbidden write function"
                    if api.mode=="drop":
                        return
                    if api.mode=="hold":
                        api.release.wait(15)
                    if api.mode=="exception":
                        pdu=bytes((function|128,2))
                    elif function in (1,2):
                        assert count==1;pdu=bytes((function,1,address%2))
                    else:
                        registers=[api.values.get(address+i,0) for i in range(count)]
                        if api.mode=="nonfinite" and count==2:
                            registers=[0x7fc0,0]
                        raw=struct.pack(">"+"H"*count,*registers);pdu=bytes((function,len(raw)))+raw
                    self.request.sendall(struct.pack(">HHHB",tid+1 if api.mode=="wrong_tid" else tid,0,len(pdu)+1,unit)+pdu)
            except (EOFError,ConnectionError,TimeoutError,OSError):
                pass
            finally:
                api.closed+=1
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address=True;daemon_threads=True
    server=Server(("127.0.0.1",0),Handler)
    api.endpoint="tcp://127.0.0.1:"+str(server.server_address[1])
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield api
    finally:
        api.release.set();server.shutdown();server.server_close();thread.join(2)


def modbus_config(api,**extra):
    return {"enabled":True,"protocol":"modbus_tcp","endpoint":api.endpoint,"allow_insecure":True,
        "start_mode":"on_command","interval_ms":250,"stale_after_ms":1000,
        "points":[{"id":"temperature","area":"holding","address":10,"type":"uint16","scale":.1,"offset":-5,"unit":"C"},
            {"id":"pressure","area":"input","address":20,"type":"float32","unit":"bar"},
            {"id":"running","area":"coil","address":1,"type":"bool"},
            {"id":"ready","area":"discrete","address":0,"type":"bool"}],**extra}


def certificate(uri,server=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"Industrial fixture only")])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri),x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False)
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=True,key_encipherment=True,
            data_encipherment=True,key_agreement=False,key_cert_sign=False,crl_sign=False,encipher_only=None,decipher_only=None),critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
        .sign(key,hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode(),key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()


@contextmanager
def opcua_server(directory,secure=True):
    from asyncua import Server,ua
    from asyncua.common.callback import CallbackType
    from asyncua.crypto.permission_rules import User,UserRole
    server_cert,server_key=certificate("urn:fixture:industrial-server",True)
    client_cert,client_key=certificate(CLIENT_URI)
    root=Path(directory);root.mkdir(exist_ok=True,parents=True)
    (root/"server.pem").write_text(server_cert);(root/"server-key.pem").write_text(server_key)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0));port=probe.getsockname()[1]
    api=SimpleNamespace(endpoint="opc.tcp://127.0.0.1:"+str(port)+"/fixture/",certificate=server_cert,
        secret=json.dumps({"certificate_pem":client_cert,"private_key_pem":client_key,"username":"fixture-user","password":"fixture-password"}),
        reads=[],writes=[],mode="ok",error=None,ready=threading.Event(),stop=threading.Event())
    class Users:
        def get_user(self,iserver,username=None,password=None,certificate=None):
            return User(role=UserRole.User) if not secure or username=="fixture-user" and password=="fixture-password" else None
    async def main():
        server=Server(user_manager=Users());await server.init()
        server.set_endpoint(api.endpoint);await server.set_application_uri("urn:fixture:industrial-server")
        await server.load_certificate(root/"server.pem");await server.load_private_key(root/"server-key.pem")
        server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt] if secure else [ua.SecurityPolicyType.NoSecurity])
        idx=await server.register_namespace("urn:fixture:industrial-points");assert idx==2
        folder=await server.nodes.objects.add_object(idx,"Fixture")
        for name,value,kind in (("temperature",23.5,ua.VariantType.Double),("running",True,ua.VariantType.Boolean)):
            node=await folder.add_variable(ua.NodeId(name,idx),name,ua.Variant(value,kind))
            def callback(nodeid,attribute,value=value,kind=kind,name=name):
                api.reads.append(name)
                now=datetime.now(timezone.utc)
                source=now-timedelta(minutes=5) if api.mode=="stale" else now+timedelta(minutes=5) if api.mode=="future" else None if api.mode=="missing_time" else now
                status=ua.StatusCode(ua.StatusCodes.BadNoCommunication if api.mode=="bad" else
                    ua.StatusCodes.Uncertain if api.mode=="uncertain" else ua.StatusCodes.Good)
                actual=ua.Variant([value],kind) if api.mode=="array" else ua.Variant(value,kind)
                return ua.DataValue(actual,StatusCode=status,SourceTimestamp=source,ServerTimestamp=now)
            server.set_attribute_value_callback(node.nodeid,callback)
        def writes(event,*_):
            if event.is_external:
                api.writes.append(event.request_params)
        server.subscribe_server_callback(CallbackType.PreWrite,writes)
        async with server:
            api.ready.set()
            while not api.stop.is_set():
                await asyncio.sleep(.03)
    def run():
        try:
            asyncio.run(main())
        except Exception as exc:
            api.error=exc;api.ready.set()
    thread=threading.Thread(target=run,daemon=True);thread.start()
    assert api.ready.wait(15),"OPC UA fixture did not start"
    if api.error:
        raise api.error
    try:
        yield api
    finally:
        api.stop.set();thread.join(5)
        assert not thread.is_alive(),"OPC UA fixture did not stop"


def opcua_config(api,**extra):
    return {"enabled":True,"protocol":"opcua","endpoint":api.endpoint,"credential_ref":REF,
        "server_certificate_pem":api.certificate,"application_uri":CLIENT_URI,
        "start_mode":"on_command","interval_ms":250,"stale_after_ms":1000,
        "points":[{"id":"temperature","node_id":"ns=2;s=temperature","type":"float64","unit":"C"},
            {"id":"running","node_id":"ns=2;s=running","type":"bool"}],**extra}
