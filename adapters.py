"""Read-only protocol adapters. No browse, method call, register write or SDK auto-reconnect."""

import asyncio
from datetime import datetime,timezone
import struct
from urllib.parse import urlsplit
from . import logic


class Modbus:
    def __init__(self,cfg,auth=None):
        self.cfg=cfg;self.reader=self.writer=None;self.sequence=0

    async def connect(self):
        target=urlsplit(self.cfg["endpoint"])
        self.reader,self.writer=await asyncio.open_connection(target.hostname,target.port,limit=1024)

    async def read(self,points):
        result=[]
        for point in points:
            function={"coil":1,"discrete":2,"holding":3,"input":4}[point["area"]]
            count=logic.TYPES[point["type"]][1]
            self.sequence=(self.sequence+1)&65535
            request=struct.pack(">HHHBBHH",self.sequence,0,6,self.cfg["unit_id"],function,point["address"],count)
            self.writer.write(request);await self.writer.drain()
            header=await self.reader.readexactly(7)
            sequence,protocol,length,unit=struct.unpack(">HHHB",header)
            if sequence!=self.sequence or protocol!=0 or unit!=self.cfg["unit_id"] or not 2<=length<=254:
                logic.fail("Unexpected Modbus response header.","protocol_error")
            pdu=await self.reader.readexactly(length-1)
            if pdu[0]==function|128 and len(pdu)==2:
                result.append({"id":point["id"],"state":"bad","value":None,"reason":"modbus_exception","status_code":pdu[1]})
                continue
            size=1 if function in (1,2) else count*2
            if len(pdu)!=size+2 or pdu[0]!=function or pdu[1]!=size:
                logic.fail("Malformed Modbus response length or function.","protocol_error")
            data=pdu[2:]
            if function in (1,2):
                if data[0]&254:
                    logic.fail("Unexpected padding bits in a one-bit Modbus read.","protocol_error")
                value=bool(data[0]&1)
            else:
                words=[data[index:index+2] for index in range(0,len(data),2)]
                if point["byte_order"]=="little":
                    words=[word[::-1] for word in words]
                if point["word_order"]=="little":
                    words.reverse()
                value=struct.unpack(">"+logic.TYPES[point["type"]][0],b"".join(words))[0]
            valid=logic.scalar(value,point["type"])
            result.append({"id":point["id"],"state":"good" if valid else "bad","value":value if valid else None,
                "reason":"invalid_scalar" if not valid else None,"status_code":0})
        return result

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()


class Opcua:
    def __init__(self,cfg,auth):
        self.cfg,self.auth=cfg,auth
        self.client=None;self.certificates=[]

    def check_certificates(self):
        now=datetime.now(timezone.utc)
        for cert in self.certificates:
            if not cert.not_valid_before_utc<=now<=cert.not_valid_after_utc:
                logic.fail("A pinned OPC UA certificate is outside its validity period.","certificate_error")

    async def connect(self):
        try:
            from asyncua import Client,ua
        except ImportError:
            logic.fail("Install asyncua 2.0.1 in the runtime interpreter for OPC UA.","dependency")
        self.client=Client(self.cfg["endpoint"],timeout=self.cfg["read_timeout_ms"]/1000,auto_reconnect=False)
        self.client.max_messagesize=1048576;self.client.max_chunkcount=32
        self.client.application_uri=self.cfg["application_uri"]
        self.client.name="BloxSmith read-only industrial telemetry"
        self.client.session_timeout=30000
        if self.cfg["opcua_security"]=="sign_encrypt":
            from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
            from asyncua.crypto.uacrypto import CertProperties
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            try:
                server=x509.load_pem_x509_certificate(self.cfg["server_certificate_pem"].encode())
                own=x509.load_pem_x509_certificate(self.auth["certificate_pem"].encode())
                self.certificates=[server,own];self.check_certificates()
                names=own.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                if self.cfg["application_uri"] not in names.get_values_for_type(x509.UniformResourceIdentifier):
                    logic.fail("Client certificate URI does not match application_uri.","certificate_error")
                pinned=server.public_bytes(serialization.Encoding.DER)
                async def validate(actual,description):
                    self.check_certificates()
                    if actual.public_bytes(serialization.Encoding.DER)!=pinned:
                        raise ua.UaStatusCodeError(ua.StatusCodes.BadCertificateUntrusted)
                    server_uris=actual.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier)
                    if description.ApplicationUri not in server_uris:
                        raise ua.UaStatusCodeError(ua.StatusCodes.BadCertificateUriInvalid)
                self.client.certificate_validator=validate
                await self.client.set_security(SecurityPolicyBasic256Sha256,
                    CertProperties(self.auth["certificate_pem"].encode(),extension="pem"),
                    CertProperties(self.auth["private_key_pem"].encode(),extension="pem",password=self.auth.get("private_key_password")),
                    server_certificate=pinned,mode=ua.MessageSecurityMode.SignAndEncrypt)
                if "username" in self.auth:
                    self.client.set_user(self.auth["username"]);self.client.set_password(self.auth["password"])
            except logic.IndustrialError:
                raise
            except Exception:
                logic.fail("Invalid OPC UA certificate or wallet credentials.","certificate_error")
        await self.client.connect()

    async def read(self,points):
        from asyncua import ua
        self.check_certificates()
        params=ua.ReadParameters()
        params.MaxAge=0;params.TimestampsToReturn=ua.TimestampsToReturn.Both
        params.NodesToRead=[ua.ReadValueId(NodeId=ua.NodeId.from_string(point["node_id"]),AttributeId=ua.AttributeIds.Value) for point in points]
        data=await self.client.uaclient.read(params)
        if len(data)!=len(points):
            logic.fail("OPC UA returned an unexpected point count.","protocol_error")
        types={"bool":ua.VariantType.Boolean,"int16":ua.VariantType.Int16,"uint16":ua.VariantType.UInt16,
            "int32":ua.VariantType.Int32,"uint32":ua.VariantType.UInt32,"int64":ua.VariantType.Int64,"uint64":ua.VariantType.UInt64,
            "float32":ua.VariantType.Float,"float64":ua.VariantType.Double,"string":ua.VariantType.String}
        result=[]
        for point,item in zip(points,data):
            state="good" if item.StatusCode.is_good() else "bad" if item.StatusCode.is_bad() else "uncertain"
            value=item.Value.Value if item.Value is not None else None
            reason="device_quality"
            if state=="good" and (item.Value is None or item.Value.VariantType!=types[point["type"]] or item.Value.is_array or not logic.scalar(value,point["type"])):
                state="bad";reason="type_mismatch"
            def stamp(value):
                return value.isoformat() if isinstance(value,datetime) else None
            result.append({"id":point["id"],"state":state,"value":value if state=="good" else None,
                "status_code":item.StatusCode.value,"reason":reason,"source_timestamp":stamp(item.SourceTimestamp),
                "server_timestamp":stamp(item.ServerTimestamp)})
        return result

    async def close(self):
        if self.client:
            await self.client.disconnect()


def adapter(cfg,auth):
    return Modbus(cfg,auth) if cfg["protocol"]=="modbus_tcp" else Opcua(cfg,auth)
