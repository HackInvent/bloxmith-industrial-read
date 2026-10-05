"""Explicit industrial read policy, point allowlists and scalar observation quality."""

from collections.abc import Mapping
from datetime import datetime,timedelta,timezone
import json
import math
import re
from urllib.parse import urlsplit
import uuid

MAX_LINE=262144
TYPES={"bool":(None,1),"int16":("h",1),"uint16":("H",1),"int32":("i",2),"uint32":("I",2),
       "int64":("q",4),"uint64":("Q",4),"float32":("f",2),"float64":("d",4),"string":(None,0)}
DEFAULTS={"enabled":False,"protocol":"modbus_tcp","endpoint":"","allow_insecure":False,
    "unit_id":1,"points":[],"start_mode":"on_command","interval_ms":1000,
    "connect_timeout_ms":10000,"read_timeout_ms":5000,"stale_after_ms":15000,
    "reconnect_attempts":2,"reconnect_delay_ms":1000,"max_measurements_per_run":100000,
    "opcua_security":"sign_encrypt","credential_ref":"","server_certificate_pem":"",
    "application_uri":"urn:hackinvent:bloxsmith:industrial-read","source_max_age_ms":30000,
    "max_clock_skew_ms":1000,"require_source_timestamp":True}


class IndustrialError(ValueError):
    def __init__(self,detail,code="invalid_request"):
        super().__init__(detail);self.code=code


def fail(detail,code="invalid_request"):
    raise IndustrialError(detail,code)


def plain(value,depth=0):
    if depth>24:
        fail("JSON nesting exceeds its limit.")
    if isinstance(value,Mapping):
        if any(not isinstance(k,str) for k in value):
            fail("Object keys must be strings.")
        return {k:plain(v,depth+1) for k,v in value.items()}
    if isinstance(value,(tuple,list)):
        return [plain(v,depth+1) for v in value]
    return value


def encoded(value):
    try:
        raw=json.dumps(plain(value),ensure_ascii=True,allow_nan=False,separators=(",",":")).encode()
        if len(raw)>MAX_LINE:
            raise ValueError()
        return raw
    except (TypeError,ValueError,RecursionError,UnicodeError):
        fail("Expected bounded finite JSON.")


def document(raw):
    if isinstance(raw,Mapping):
        raw=encoded(raw)
    if isinstance(raw,str):
        raw=raw.encode("utf-8",errors="strict")
    if not isinstance(raw,bytes) or len(raw)>MAX_LINE:
        fail("Expected bounded JSON object.")
    def pairs(items):
        result={}
        for key,item in items:
            if key in result:
                fail("Duplicate JSON key.")
            result[key]=item
        return result
    try:
        value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail("Finite JSON required."))
        if not isinstance(value,dict):
            raise ValueError()
        return plain(value)
    except (ValueError,TypeError,RecursionError,UnicodeError):
        fail("Expected one JSON object without duplicate keys.")


def text(value,limit=128,empty=False):
    if not isinstance(value,str) or len(value)>limit or (not value and not empty) or any(ord(c)<32 for c in value):
        fail("Expected bounded text without control characters.")
    return value


def ident(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}",value):
        fail("Use a 1–64 character ASCII identifier.")
    return value


def integer(value,lo,hi):
    if type(value) is not int or not lo<=value<=hi:
        fail(f"Expected an integer between {lo} and {hi}.")
    return value


def number(value):
    if type(value) not in (float,int) or not math.isfinite(value) or abs(value)>1e12:
        fail("Calibration must be a finite number between -1e12 and 1e12.")
    return value


def configuration(raw):
    if not isinstance(raw,Mapping) or set(raw)-set(DEFAULTS)-{"execution","runtime_path","runtime_path_label"}:
        fail("Unknown setting; only read-only industrial adapters are supported.")
    cfg=plain({**DEFAULTS,**{k:v for k,v in raw.items() if k in DEFAULTS}})
    for key in ("enabled","allow_insecure","require_source_timestamp"):
        if type(cfg[key]) is not bool:
            fail("Enable choices must be booleans.")
    if cfg["protocol"] not in ("modbus_tcp","opcua") or cfg["opcua_security"] not in ("sign_encrypt","none") or cfg["start_mode"] not in ("periodic","on_command"):
        fail("Unsupported protocol, security policy or acquisition mode.")
    endpoint=text(cfg["endpoint"],512,True)
    if endpoint:
        try:
            parsed=urlsplit(endpoint)
            expected="tcp" if cfg["protocol"]=="modbus_tcp" else "opc.tcp"
            if parsed.scheme!=expected or not parsed.hostname or not parsed.port or not 1<=parsed.port<=65535 or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError()
            if cfg["protocol"]=="modbus_tcp" and parsed.path not in ("","/"):
                raise ValueError()
            if any(c.isspace() for c in endpoint) or "\\" in endpoint or "%" in parsed.netloc:
                raise ValueError()
        except (ValueError,TypeError):
            fail("Use tcp://host:port for Modbus or opc.tcp://host:port/path for OPC UA, without credentials.")
    integer(cfg["unit_id"],1,255)
    for key,lo,hi in (("interval_ms",250,3600000),("connect_timeout_ms",500,30000),("read_timeout_ms",100,30000),
        ("stale_after_ms",250,86400000),("source_max_age_ms",250,86400000),("max_clock_skew_ms",0,60000),
        ("reconnect_attempts",0,10),("reconnect_delay_ms",250,60000),("max_measurements_per_run",1,1000000)):
        integer(cfg[key],lo,hi)
    text(cfg["application_uri"],256)
    if not cfg["application_uri"].startswith("urn:"):
        fail("OPC UA application URI must match the client certificate URI.")
    reference=text(cfg["credential_ref"],512,True)
    if reference and not reference.startswith("secret://"):
        fail("Use a wallet reference, never a password or private key.")
    cert=cfg["server_certificate_pem"]
    if not isinstance(cert,str) or len(cert)>16384 or cert and (not cert.startswith("-----BEGIN CERTIFICATE-----") or "PRIVATE KEY" in cert):
        fail("Server certificate must be one public PEM certificate, never a private key.")
    points=cfg["points"]
    if not isinstance(points,list) or len(points)>64:
        fail("Configure up to 64 explicit points.")
    ids=set();normalized=[]
    for point in points:
        if not isinstance(point,dict):
            fail("Each point must be an object.")
        allowed={"id","type","unit","scale","offset"} | ({"area","address","byte_order","word_order"} if cfg["protocol"]=="modbus_tcp" else {"node_id"})
        if set(point)-allowed or not {"id","type"}<=set(point):
            fail("Invalid point fields for the selected protocol.")
        item={"unit":"","scale":1,"offset":0,**point}
        ident(item["id"])
        if item["id"] in ids or not isinstance(item["type"],str) or item["type"] not in TYPES:
            fail("Point IDs must be distinct and types supported.")
        ids.add(item["id"]);text(item["unit"],32,True)
        number(item["scale"]);number(item["offset"])
        if item["type"] in ("bool","string") and (item["scale"]!=1 or item["offset"]!=0):
            fail("Boolean and string points cannot be numerically calibrated.")
        if cfg["protocol"]=="modbus_tcp":
            if item.get("area") not in ("coil","discrete","holding","input") or item["type"]=="string":
                fail("Modbus supports coil/discrete booleans and holding/input numeric registers.")
            if (item["area"] in ("coil","discrete")) != (item["type"]=="bool"):
                fail("Bit areas require bool; register areas require a numeric type.")
            count=TYPES[item["type"]][1]
            integer(item.get("address"),0,65536-count)
            item.setdefault("byte_order","big");item.setdefault("word_order","big")
            if item["byte_order"] not in ("big","little") or item["word_order"] not in ("big","little"):
                fail("Byte and word order must be big or little.")
        else:
            node=text(item.get("node_id"),512)
            if not re.fullmatch(r"ns=(?:0|[1-9][0-9]{0,4});(?:i=[0-9]{1,10}|s=.+)",node):
                fail("Use an explicit OPC UA ns=N;i=ID or ns=N;s=NAME; no browse paths or remote servers.")
            ns=int(node.split(";",1)[0][3:])
            if ns>65535 or node.split(";",1)[1].startswith("i=") and int(node.split(";i=",1)[1])>4294967295:
                fail("OPC UA NodeId exceeds its protocol range.")
        normalized.append(item)
    cfg["points"]=normalized
    if cfg["enabled"]:
        if not endpoint or not points:
            fail("Configure the endpoint and at least one point before enabling reads.")
        insecure=cfg["protocol"]=="modbus_tcp" or cfg["opcua_security"]=="none"
        if insecure and not cfg["allow_insecure"]:
            fail("Explicitly allow unencrypted access on the trusted equipment network.")
        if cfg["protocol"]=="opcua" and cfg["opcua_security"]=="sign_encrypt" and (not cert or not reference):
            fail("Secure OPC UA needs a pinned server PEM certificate and wallet client credentials.")
    if cfg["protocol"]=="opcua" and cfg["opcua_security"]=="none" and reference:
        fail("Never send wallet credentials over an unencrypted OPC UA channel.")
    return cfg


def command(raw,cfg):
    item=document(raw)
    if set(item)-{"action","request_id","points"} or item.get("action") not in ("read","start","stop","status"):
        fail("Commands are read, start, stop or status; no writes, method calls or endpoint overrides.")
    if "request_id" in item:
        ident(item["request_id"])
    if "points" in item:
        selected=item["points"]
        if item["action"]!="read" or not isinstance(selected,list) or not selected or any(not isinstance(v,str) for v in selected) or len(set(selected))!=len(selected) or set(selected)-{p["id"] for p in cfg["points"]}:
            fail("read.points must select distinct configured point IDs.")
    return item


def credentials(cfg,resolver):
    if cfg["protocol"]!="opcua" or cfg["opcua_security"]!="sign_encrypt":
        return None
    try:
        secret=document(resolver(cfg["credential_ref"]))
        if set(secret)-{"certificate_pem","private_key_pem","private_key_password","username","password"} or not {"certificate_pem","private_key_pem"}<=set(secret):
            raise ValueError()
        if ("username" in secret)!=("password" in secret):
            raise ValueError()
        if any(not isinstance(v,str) or not v or len(v)>16384 for v in secret.values()):
            raise ValueError()
        if not secret["certificate_pem"].startswith("-----BEGIN CERTIFICATE-----") or not secret["private_key_pem"].startswith("-----BEGIN "):
            raise ValueError()
        return secret
    except Exception:
        fail("Unlock the wallet and provide client certificate_pem/private_key_pem, optionally username/password.","credentials")


def scalar(value,kind):
    if kind=="bool":
        return type(value) is bool
    if kind=="string":
        return isinstance(value,str) and len(value)<=256 and all(ord(c)>=32 or c in "\n\t" for c in value)
    if kind.startswith(("int","uint")):
        bits=int(kind.lstrip("uint"));signed=kind.startswith("int")
        return type(value) is int and (-(2**(bits-1)) if signed else 0)<=value<(2**(bits-1) if signed else 2**bits)
    return type(value) in (int,float) and math.isfinite(value)


def observation(raw,cfg,selected,request_id=None):
    points={point["id"]:point for point in cfg["points"]}
    if not isinstance(raw,list) or len(raw)!=len(selected) or [v.get("id") for v in raw]!=selected:
        fail("Adapter returned unexpected points.","invalid_response")
    now=datetime.now(timezone.utc);values=[]
    for item in raw:
        point=points[item["id"]];flags=[];state=item.get("state")
        if state not in ("good","bad","uncertain","error"):
            fail("Invalid observation quality.","invalid_response")
        value=item.get("value");source=item.get("source_timestamp");age=None
        if state!="good":
            flags.append(item.get("reason","device_quality"));value=None
        elif not scalar(value,point["type"]):
            flags.append("type_mismatch");value=None
        if cfg["protocol"]=="opcua":
            if source:
                try:
                    instant=datetime.fromisoformat(source)
                    if instant.tzinfo is None:
                        raise ValueError()
                    age=(now-instant.astimezone(timezone.utc)).total_seconds()*1000
                    if age < -cfg["max_clock_skew_ms"]:
                        flags.append("source_clock_ahead")
                    elif age>cfg["source_max_age_ms"]:
                        flags.append("source_stale")
                except (ValueError,TypeError,OverflowError):
                    flags.append("invalid_source_timestamp")
            elif cfg["require_source_timestamp"]:
                flags.append("source_timestamp_missing")
        raw_value=value
        if not flags and point["type"] not in ("bool","string"):
            value=value*point["scale"]+point["offset"]
            if not math.isfinite(value):
                flags.append("calibration_overflow")
        valid=not flags
        values.append({"id":point["id"],"value":value if valid else None,"raw_value":raw_value if valid else None,
            "unit":point["unit"],"observed_at":now.isoformat(),"source_timestamp":source,
            "server_timestamp":item.get("server_timestamp"),"source_age_ms":round(age,3) if age is not None else None,
            "source_age_known":age is not None,"expires_at":(now+timedelta(milliseconds=cfg["stale_after_ms"])).isoformat(),
            "quality":{"valid":valid,"state":("observed" if cfg["protocol"]=="modbus_tcp" else "good") if valid else
                ("stale" if "source_stale" in flags else state if state!="good" else "unknown"),
                "flags":flags,"status_code":item.get("status_code")}})
    return {"event":"measurement","measurement_id":uuid.uuid4().hex,"request_id":request_id,
        "protocol":cfg["protocol"],"observed_at":now.isoformat(),"atomic_snapshot":False,"points":values}
