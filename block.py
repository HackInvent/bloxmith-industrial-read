"""Read-only industrial observations through public runtime, wallet and listener services."""

import time
from bloxsmith_app.block_api import APPLICATION_JSON,BlockDefinition,BlockRuntimeOutput,BlockRuntimePreparation,BlockRuntimeResult
from . import engine,logic,ui


# FB1 - Scope every command to configured read-only points and explicit secure/insecure endpoints.
# FB2 - Read real Modbus functions 1/2/3/4 and OPC UA scalar Value/quality/timestamps only.
# FB3 - Calibrate valid values and explicitly invalidate bad, disconnected and stale observations.
# FB4 - Own a single bounded connection process; limit rate/reconnect/Stop and fence parent death.
# FB5 - Both runtimes, wallet credentials, stable ports and isolated managed/linked packages.
# FB6 - Responsive translated owned forms, protocol-specific controls and Apply/Cancel semantics.
class IndustrialReadBlock(BlockDefinition):
    kind="industrial_read"

    def ui_assets(self,surface="modal"):
        return list(self.model.get("ui_assets",{}).get(surface,[]))

    def prepare_runtime(self,context):
        logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode=="zeromq_active")

    def result(self,context,values):
        return BlockRuntimeResult(status="success",outputs=[BlockRuntimeOutput(port_id=int(port.id),port_name=port.name,
            value=logic.encoded(values[port.name]).decode(),content_type=APPLICATION_JSON)
            for port in context.output_ports if port.name in values])

    def execute_runtime(self,context):
        try:
            cfg=logic.configuration(context.config)
            names={int(port.id):port.name for port in context.input_ports}
            items=[(names.get(event.input_port_id),event.value) for event in context.input_events] if context.input_events else [
                (name,context.input_value(name,str(ident))) for ident,name in names.items() if context.has_input_value(name,str(ident))]
            if not items:
                return BlockRuntimeResult(status="skipped",outputs=[])
            if len(items)!=1 or items[0][0]!="command":
                logic.fail("Send one industrial command per activation.")
            item=logic.command(items[0][1],cfg)
            if context.runtime_mode!="zeromq_active":
                if item["action"]!="read":
                    return self.result(context,{"status":{"code":"preview","live":False,
                        "detail":"One Shot supports explicit read only. Continuous acquisition requires Active Runtime."}})
                values={};listener=engine.Listener({**cfg,"start_mode":"on_command"},values.update,context.services.get("resolve_secret"))
                try:
                    listener.command(item)
                    deadline=time.monotonic()+(cfg["connect_timeout_ms"]+cfg["read_timeout_ms"])/1000+1
                    while listener.pending and time.monotonic()<deadline:
                        if context.services.get("cancel_requested",lambda:False)():
                            values={"status":{"code":"cancelled"}};break
                        listener.tick(time.monotonic());time.sleep(.01)
                    if listener.pending and values.get("status",{}).get("code")!="cancelled":
                        listener.failure("timeout",time.monotonic())
                finally:
                    listener.close()
                return self.result(context,values)
            client=context.services.get("runtime_listener")
            if client is None:
                logic.fail("The listener command bridge is unavailable.")
            client.send(item)
            return BlockRuntimeResult(status="success",outputs=[])
        except (ValueError,TypeError,RuntimeError,OSError) as exc:
            return self.result(context,{"status":{"code":"command_rejected",
                "detail":str(exc) if isinstance(exc,logic.IndustrialError) else "Industrial command unavailable."}})

    def listen_runtime(self,context):
        cfg=logic.configuration(context.config)
        def emit(values):
            if not context.stop_requested():
                context.emit_result(self.result(context,values))
        listener=None
        try:
            listener=engine.Listener(cfg,emit,context.services.get("resolve_secret"))
            while not context.stop_requested():
                received=context.receive_command(timeout_sec=.02)
                if received is not None:
                    try:
                        listener.command(logic.command(dict(received.payload),cfg))
                    except (ValueError,TypeError,OSError):
                        emit({"status":{"code":"command_rejected"}})
                listener.tick(time.monotonic())
        except logic.IndustrialError as exc:
            emit({"status":{"code":"configuration_error","detail":str(exc)}})
            while not context.stop_requested():
                context.receive_command(timeout_sec=.1)
        finally:
            if listener:
                listener.close()

    def handle_ui_action(self,*,node,action,values,payload=None):
        if action in {"save_settings","modal_update_fields","inspector_update_fields"}:
            patch=(values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(),**(node.get("config") or {}),**patch["config"]})
                except (ValueError,TypeError) as exc:
                    return {"error":self.translate("block.industrial_read.error",{"detail":str(exc)},fallback="Invalid settings: {detail}")}
        return ui.replace_config(super().handle_ui_action(node=node,
            action="modal_update_fields" if action=="save_settings" else action,values=values,payload=payload),node)

    def render_modal(self,*,node,payload=None):
        return ui.modal(self,node,payload)

    def render_inspector_panel(self,*,node,payload=None):
        return ui.inspector(self,node,payload)

    def render_node_card(self,*,node,payload=None):
        cfg={**self.default_config(),**(node.get("config") or {})}
        key="enabled" if cfg["enabled"] else "disabled"
        return ui.card(self,node,cfg["protocol"]+" · "+self.translate("block.industrial_read."+key,fallback=key))
