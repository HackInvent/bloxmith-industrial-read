"""Owned read-only connection, bounded pipes and parent-death fencing; no framework imports."""

import asyncio
import ctypes
import logging
import os
from pathlib import Path
import resource
import signal
import sys
import types

package=types.ModuleType("industrial_owned");package.__path__=[str(Path(__file__).resolve().parent)]
sys.modules["industrial_owned"]=package
from industrial_owned import adapters,logic


def emit(value):
    raw=logic.encoded(value)+b"\n"
    while raw:
        raw=raw[os.write(1,raw):]


async def run():
    reader=asyncio.StreamReader(limit=logic.MAX_LINE+1)
    transport,_=await asyncio.get_running_loop().connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin.buffer)
    client=None
    try:
        first=await reader.readline()
        cfg_auth=logic.document(first);cfg=logic.configuration(cfg_auth["config"])
        if not cfg["enabled"]:
            logic.fail("Industrial reads are disabled.","disabled")
        client=adapters.adapter(cfg,cfg_auth.get("auth"))
        await asyncio.wait_for(client.connect(),cfg["connect_timeout_ms"]/1000)
        emit({"type":"ready"})
        by_id={point["id"]:point for point in cfg["points"]}
        while True:
            raw=await reader.readline()
            if not raw:
                return
            message=logic.command(raw,cfg)
            if message["action"]=="stop":
                return
            if message["action"]!="read":
                logic.fail("Unexpected helper command.","protocol_error")
            points=[by_id[ident] for ident in message.get("points",list(by_id))]
            result=await asyncio.wait_for(client.read(points),cfg["read_timeout_ms"]/1000)
            emit({"type":"read","points":result})
    finally:
        transport.close()
        if client:
            try:
                await asyncio.wait_for(client.close(),.3)
            except Exception:
                pass


def main():
    if sys.platform!="linux" or len(sys.argv)!=2:
        raise SystemExit(2)
    parent=int(sys.argv[1]);libc=ctypes.CDLL(None,use_errno=True)
    libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
    libc.prctl.restype=ctypes.c_int
    if os.getppid()!=parent or libc.prctl(1,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=parent:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run())
    except Exception as exc:
        emit({"type":"error","code":exc.code if isinstance(exc,logic.IndustrialError) else
            "timeout" if isinstance(exc,TimeoutError) else "connection_error"})
        raise SystemExit(1) from None


if __name__=="__main__":
    main()
