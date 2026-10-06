# Industrial Read

[![Industrial Read](media/thumbnail.webp)](media/cover.png)

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

## Role

Read explicitly configured machine points over **Modbus TCP or OPC UA**, convert their values and publish timestamped JSON observations. This block never writes coils/registers, changes machine settings, browses for points, calls equipment methods or issues actuator commands. It is telemetry software, not a certified safety controller.

Reads are disabled by default. Configure an authorized endpoint and a point allowlist, then enable the block. Preparation and editing settings never connect to equipment. Do not connect tests to production machines.

## Ports and commands

The **`command`** data input accepts one JSON command per activation:

```json
{"action":"read","request_id":"sample-1","points":["temperature"]}
```

`points` is optional and selects only existing point IDs. It cannot supply new addresses or change the endpoint. Supported actions:

| Action | Behavior |
| --- | --- |
| `read` | Read a single batch, all configured points or the requested subset. |
| `start` | Start periodic acquisition in Active Runtime. |
| `stop` | Stop acquisition, close the connection and invalidate retained observations. |
| `status` | Report local state without opening a connection. |

`request_id` is optional. Active Runtime remembers the latest 256 accepted IDs for this Run: identical repeats are ignored, conflicting reuse is refused. This is not durable or global deduplication. Requests arriving during a read or before the minimum interval are rejected with `busy` or `rate_limited`; they are not queued silently.

| Output | Contents |
| --- | --- |
| `measurements` | `measurement` or `invalidated` event; point IDs, calibrated values, units, observation/source timestamps, expiration and per-point quality. |
| `status` | Connection/acquisition state, counters, retry decisions and safe error codes. |

Bad, uncertain, stale, missing or incorrectly typed values become **`null` with `quality.valid: false`**, never an apparently healthy zero. Calibration is `raw_value * scale + offset`. A batch is not an atomic machine snapshot. Downstream blocks must check quality and expiration before using a value; cached graph values are not proof of current machine state.

## Modbus TCP

Set an endpoint such as `tcp://machine.example:502`, a unit ID from 1 to 255 and explicit **zero-based** addresses. Unit 0 is refused. Only functions 1/2/3/4 are implemented:

```json
[
  {"id":"temperature","area":"holding","address":10,"type":"int16","scale":0.1,"offset":0,"unit":"C"},
  {"id":"pressure","area":"input","address":20,"type":"float32","byte_order":"big","word_order":"little","unit":"bar"},
  {"id":"running","area":"coil","address":1,"type":"bool"}
]
```

Areas: `coil`, `discrete`, `holding`, `input`. Register types: signed/unsigned 16/32/64-bit integers and 32/64-bit floats. Bit areas require `bool`. Byte order applies inside each 16-bit word; word order applies across words. Strings, RTU/serial and Modbus Security/TLS are not implemented.

Modbus TCP is **unencrypted**: explicitly enable **Allow unencrypted equipment access** and use only an authorized trusted network. A successful reply says the register was read, not that the physical sensor is fresh: Modbus supplies no source timestamp here, so `source_age_known` is false and quality is `observed`, not a claim of validated hardware health.

## OPC UA

Requires **asyncua 2.0.1** in the Python environment that runs BloxSmith and its package hosts; the block does not install dependencies automatically. See `requirements.txt`. Modbus uses the standard library and does not require asyncua.

Use an explicit endpoint such as `opc.tcp://machine.example:4840/plant/` and point NodeIds; no endpoint discovery or namespace browsing is performed:

```json
[
  {"id":"temperature","node_id":"ns=2;s=Temperature","type":"float64","unit":"C"},
  {"id":"running","node_id":"ns=2;i=1002","type":"bool"}
]
```

Types also include `string` (maximum 256 characters); arrays are refused. Reads request the Value attribute, both source/server timestamps and `MaxAge=0`. Source timestamps are required by default. Excessively old or future-dated source values are invalidated; synchronize machine/server clocks and choose the permitted age/skew explicitly.

The default security profile is **Basic256Sha256, SignAndEncrypt**, with the exact public server certificate pinned in configuration. The client certificate application URI must match **Application URI**. Both certificate validity periods and the server application URI are checked. No automatic insecure fallback is attempted. Certificate rotation requires an explicit configuration change.

Place client credentials in a wallet JSON secret and configure only its `secret://workspace/...` reference:

```json
{
  "certificate_pem":"-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----",
  "private_key_pem":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
  "username":"equipment-reader",
  "password":"WALLET_ONLY"
}
```

Username/password are optional as a pair. `private_key_password` is optional for an encrypted key. Never put private keys/passwords in a blueprint, URL or README. The server must trust the client certificate and grant read-only access. The block pins one server certificate, rather than implementing a configurable CA/CRL trust store. Server-side authorization remains essential.

Anonymous unencrypted OPC UA is available only with security `none` and explicit insecure access enabled. A wallet reference is refused in that mode; credentials are never intentionally sent through the insecure profile.

## Execution and lifecycle

- **Active Runtime:** on-command acquisition, or periodic reads starting at Play. One connection and one batch at a time; the next interval starts after completion. No catch-up bursts. Reconnect attempts, rate, deadlines and total batches are bounded.
- **Simulation/One Shot:** an explicit `read` performs a real read. Other commands return a non-live preview; continuous acquisition requires Active Runtime.
- Stop closes/kills the owned connection helper; Linux parent-death fencing prevents the helper surviving a killed package host. A disconnected or expired reading emits an explicit invalidation while the Run is live. A stopped Run cannot deliver more events: downstream consumers must also honor Run lifecycle and `expires_at`.
- Network errors emit status/invalidations without intentionally failing the whole workflow. Terminal configuration/protocol errors are not retried automatically. The finite reconnect budget is per listener, not reset after each successful sample.
- No equipment values or credentials are persisted by the block. Secrets are resolved at connection time, passed through a private pipe and never placed in command arguments. The owned process has time/memory bounds, but is not an OS security sandbox.

Linux and Python 3.10+ are required. A node allows up to 64 scalar points. Configure deadlines for the complete batch, not just one register. Modal and inspector use the same draft configuration, protocol-dependent fields, Apply/Cancel and English/French labels. Reload a prepared Run after changing settings.

## Tests

Tests use disposable loopback Modbus servers and a real local asyncua server with ephemeral certificates. No machine, production account or network scan is involved. Tests cover read-only functions, scalar conversion, calibration, quality/staleness, certificate pinning, retry/rate bounds, Stop, both runtime modes and managed/linked package UI. The private BloxSmith framework test harness is not redistributed.

## License and references

Block code: Apache-2.0. The optional asyncua dependency is LGPL-3.0-or-later; it is installed separately, not bundled. Other dependencies keep their own licenses.

- [Modbus specifications](https://www.modbus.org/modbus-specifications)
- [OPC UA DataValue contract](https://reference.opcfoundation.org/specs/OPC-10000-4/7.11)
- [asyncua project](https://github.com/FreeOpcUa/opcua-asyncio)
