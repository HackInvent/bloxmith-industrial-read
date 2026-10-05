"""Single-flight acquisition with explicit invalidation, bounded reconnect and owned process IO."""

from collections import OrderedDict
from datetime import datetime,timezone
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from . import logic


class Connection:
    def __init__(self,cfg,auth):
        self.process=subprocess.Popen([sys.executable,"-E","-B",str(Path(__file__).with_name("worker.py")),str(os.getpid())],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,close_fds=True,
            env={k:v for k,v in os.environ.items() if k in {"PATH","LANG","LC_ALL"}})
        self.outgoing=bytearray(logic.encoded({"config":cfg,"auth":auth})+b"\n");self.incoming=bytearray()
        os.set_blocking(self.process.stdin.fileno(),False);os.set_blocking(self.process.stdout.fileno(),False)

    def send(self,item):
        raw=logic.encoded(item)+b"\n"
        if len(self.outgoing)+len(raw)>logic.MAX_LINE*2:
            logic.fail("Adapter command buffer is full.","buffer_limit")
        self.outgoing.extend(raw)

    def poll(self):
        if self.outgoing:
            try:
                count=os.write(self.process.stdin.fileno(),self.outgoing)
                del self.outgoing[:count]
            except BlockingIOError:
                pass
        result=[]
        for _ in range(5):
            try:
                chunk=os.read(self.process.stdout.fileno(),65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.incoming.extend(chunk)
            while b"\n" in self.incoming:
                line,_,rest=self.incoming.partition(b"\n");self.incoming=bytearray(rest)
                result.append(logic.document(bytes(line)))
            if len(self.incoming)>logic.MAX_LINE:
                logic.fail("Oversized adapter response.","invalid_response")
        if not result and self.process.poll() is not None:
            logic.fail("Industrial connection ended.","connection_lost")
        return result

    def close(self):
        if self.process.poll() is None:
            # Closing stdin requests a protocol disconnect; the deadline also covers stuck IO.
            self.process.stdin.close()
            try:
                self.process.wait(timeout=.4)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=.4)
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.stdout.close()


class Listener:
    def __init__(self,cfg,emit,resolver=None,factory=Connection):
        self.cfg,self.emit,self.resolver,self.factory=cfg,emit,resolver,factory
        self.child=None;self.pending=None;self.state="idle" if cfg["enabled"] else "disabled"
        self.periodic=cfg["enabled"] and cfg["start_mode"]=="periodic"
        self.started=self.next_at=self.min_next=0;self.retries=self.count=0
        self.seen=OrderedDict();self.fresh={};self.report(self.state)

    def report(self,code,**extra):
        self.emit({"status":{"code":code,"state":self.state,"periodic":self.periodic,
            "measurements":self.count,"reconnects":self.retries,**extra}})

    def invalidate(self,reason,ids=None,request_id=None):
        chosen=ids if ids is not None else list(self.fresh)
        if not chosen:
            return
        points={p["id"]:p for p in self.cfg["points"]};now=datetime.now(timezone.utc).isoformat()
        self.emit({"measurements":{"event":"invalidated","measurement_id":uuid.uuid4().hex,
            "protocol":self.cfg["protocol"],"observed_at":now,"atomic_snapshot":False,"request_id":request_id,
            "points":[{"id":ident,"value":None,"unit":points[ident]["unit"],"observed_at":now,
                "quality":{"valid":False,"state":"stale" if reason=="stale" else "unknown","flags":[reason]}}
                for ident in chosen]}})
        for ident in chosen:
            self.fresh.pop(ident,None)

    def discard(self):
        if self.child:
            self.child.close();self.child=None

    def failure(self,reason,now):
        pending=self.pending;self.pending=None
        self.discard()
        self.invalidate(reason,list(dict.fromkeys([*self.fresh,*(pending["points"] if pending else [])])),pending.get("request_id") if pending else None)
        terminal=reason in {"certificate_error","credentials","dependency","protocol_error","invalid_response"}
        retry=self.periodic and not terminal and self.retries<self.cfg["reconnect_attempts"]
        if retry:
            self.retries+=1;self.state="retry_wait";self.next_at=now+self.cfg["reconnect_delay_ms"]/1000
        else:
            self.periodic=False;self.state="disconnected"
        self.report("read_error",reason=reason,request_id=pending.get("request_id") if pending else None,retry_scheduled=retry)

    def begin(self,now,item):
        self.pending={"action":"read","points":item.get("points",[p["id"] for p in self.cfg["points"]]),
            **({"request_id":item["request_id"]} if "request_id" in item else {})}
        self.started=now;self.min_next=now+self.cfg["interval_ms"]/1000
        try:
            if self.child is None:
                auth=logic.credentials(self.cfg,self.resolver)
                self.child=self.factory(self.cfg,auth);self.state="connecting"
            else:
                self.child.send(self.pending);self.state="reading"
            self.report(self.state,request_id=item.get("request_id"))
        except (OSError,ValueError) as exc:
            self.failure(exc.code if isinstance(exc,logic.IndustrialError) else "connection_unavailable",now)

    def command(self,item,now=None):
        now=time.monotonic() if now is None else now
        action,ident=item["action"],item.get("request_id")
        signature=logic.encoded(item)
        if action=="status":
            self.report(self.state,request_id=ident);return
        if ident in self.seen:
            self.report("duplicate" if self.seen[ident]==signature else "request_id_conflict",request_id=ident);return
        if action=="stop":
            self.periodic=False;self.pending=None;self.discard();self.invalidate("stopped")
            self.state="idle" if self.cfg["enabled"] else "disabled";self.report("stopped",request_id=ident)
        elif not self.cfg["enabled"]:
            self.report("disabled",request_id=ident);return
        elif self.count>=self.cfg["max_measurements_per_run"]:
            self.report("measurement_limit",request_id=ident);return
        elif action=="start":
            if not self.periodic:
                self.periodic=True;self.retries=0;self.next_at=max(now,self.min_next)
            self.report("started",request_id=ident)
        elif self.pending:
            self.report("busy",request_id=ident);return
        elif now<self.min_next:
            self.report("rate_limited",request_id=ident);return
        else:
            self.begin(now,item)
        if ident:
            self.seen[ident]=signature
            if len(self.seen)>256:
                self.seen.popitem(last=False)

    def tick(self,now):
        expired=[ident for ident,expiry in self.fresh.items() if now>=expiry]
        if expired:
            self.invalidate("stale",expired);self.report("stale",points=expired)
        if self.child:
            try:
                if self.pending and now-self.started>(self.cfg["connect_timeout_ms"] if self.state=="connecting" else self.cfg["read_timeout_ms"])/1000+.2:
                    self.failure("timeout",now);return
                for event in self.child.poll():
                    if event.get("type")=="error":
                        self.failure(event.get("code","connection_error"),now);return
                    if event.get("type")=="ready" and self.state=="connecting" and self.pending:
                        self.child.send(self.pending);self.state="reading";self.started=now
                        self.report("connected");continue
                    if event.get("type")!="read" or self.state!="reading" or not self.pending:
                        logic.fail("Unexpected adapter event.","invalid_response")
                    data=logic.observation(event["points"],self.cfg,self.pending["points"],self.pending.get("request_id"))
                    self.pending=None;self.count+=1;self.state="connected"
                    for point in data["points"]:
                        if point["quality"]["valid"]:
                            self.fresh[point["id"]]=now+self.cfg["stale_after_ms"]/1000
                        else:
                            self.fresh.pop(point["id"],None)
                    self.emit({"measurements":data});self.report("measured",valid=all(p["quality"]["valid"] for p in data["points"]))
                    self.next_at=now+self.cfg["interval_ms"]/1000
                    if self.count>=self.cfg["max_measurements_per_run"]:
                        self.periodic=False;self.discard();self.state="measurement_limit";self.report("measurement_limit")
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self.failure(exc.code if isinstance(exc,logic.IndustrialError) else "invalid_response",now)
        if self.periodic and not self.pending and now>=max(self.next_at,self.min_next):
            self.begin(now,{"action":"read"})

    def close(self):
        self.periodic=False;self.pending=None;self.discard()
