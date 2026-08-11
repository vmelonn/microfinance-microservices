# IBM ACE, ISO 8583 gateway

These artifacts implement `Iso8583Library/wsdl/Iso8583Gateway.wsdl` on IBM App
Connect Enterprise. `services/ace-stub/` implements the same WSDL in Python
and is what runs today.

## Status, stated plainly

| Artifact | State |
|---|---|
| `Iso8583Library/wsdl/Iso8583Gateway.wsdl` | **Complete and in use.** ace-stub serves this exact file; the platform's tests run against it. |
| `Iso8583Library/dfdl/ISO8583.xsd` | **Complete, not executed.** Correct DFDL, cross-checked against the Python codec by hand. Never run through an ACE parser. |
| `esql/SoapToIso.esql` | **Complete, not compiled.** |
| `esql/IsoToSoap.esql` | **Complete, not compiled.** |
| `Iso8583Gateway.msgflow` | **Specified below, not authored as a file.** |
| `Dockerfile` | **Complete, never built**, the base image needs an entitlement key. |

Nothing here has been run. The IBM entitlement had not come through, so
there is no Toolkit to compile a BAR with and no entitled base image to pull.
Treat these as ready-to-import, not as verified-working.

**The msgflow is deliberately not a hand-written file.** A `.msgflow` is
Eclipse EMF XMI with internal identifiers and layout data. Hand-authoring one
produces a file that either fails to open or opens subtly wrong, which is
worse than not shipping it, so the node graph is specified precisely below
and should be assembled in the Toolkit, where the connections get validated.

## Why the platform does not wait for any of this

`iso8583-adapter` talks to whatever `ISO8583_SOAP_ENDPOINT` names. Today that
is `ace-stub`, which serves the same WSDL, accepts the same envelopes, and
does the same mediation in Python, real BCD packing, a real bitmap, a real
MLI-framed TCP socket to the switch. It is a stand-in, not a mock.

So the SOAP boundary is exercised end-to-end from day one, and ACE arrives
into a slot whose shape is already proven. The alternative, building the
SOAP layer only once the licence landed, would have left the riskiest
integration in the platform untested until the latest possible moment.

## The message flow

```
                    ┌──────────────────────┐
   SOAP/HTTP  ─────▶│ SOAP Input           │  /Iso8583Gateway
   from adapter     │ Iso8583GatewayPort   │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │ SOAP Extract         │  envelope → body
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │ Compute: SoapToIso   │  esql/SoapToIso.esql
                    │ SOAP body → ISO tree │  assigns DE 11 (STAN)
                    └──────────┬───────────┘
                               │  DFDL domain
                    ┌──────────▼───────────┐
                    │ TCPIP Client Output  │  MLI-framed, 2-byte big-endian
                    └──────────┬───────────┘
                               │
                          [ the switch ]
                               │
                    ┌──────────▼───────────┐
                    │ TCPIP Client Receive │──── Timeout ──┐
                    └──────────┬───────────┘               │
                               │  raw bytes                │
                    ┌──────────▼───────────┐    ┌──────────▼─────────────┐
                    │ ResetContentDescr.   │    │ Compute:               │
                    │ parse via DFDL       │    │ HandleSwitchTimeout    │
                    └──────────┬───────────┘    │ → 0400 reversal        │
                               │                │ → SOAP fault           │
                    ┌──────────▼───────────┐    │   SWITCH_TIMEOUT       │
                    │ Compute: IsoToSoap   │    └──────────┬─────────────┘
                    │ ISO tree → SOAP body │               │
                    └──────────┬───────────┘               │
                               │                           │
                    ┌──────────▼───────────────────────────▼──┐
                    │ SOAP Reply                              │
                    └─────────────────────────────────────────┘
```

### Node configuration

| Node | Property | Value |
|---|---|---|
| SOAP Input | WSDL | `Iso8583Library/wsdl/Iso8583Gateway.wsdl` |
| | Port | `Iso8583GatewayPort` |
| | Path suffix | `/Iso8583Gateway` |
| Compute (SoapToIso) | ESQL module | `gateway.iso8583.SoapToIso` |
| | Compute mode | Message |
| TCPIP Client Output | Connection | `{SWITCH_HOST}:{SWITCH_PORT}` |
| | Record | Fixed-length header, 2 bytes, big-endian |
| TCPIP Client Receive | Timeout | 10s → Timeout terminal |
| ResetContentDescriptor | Domain | `DFDL` |
| | Message type | `{urn:microfinance:iso8583:dfdl:v1}:ISO8583Message` |
| Compute (IsoToSoap) | ESQL module | `gateway.iso8583.IsoToSoap` |
| Compute (timeout) | ESQL module | `gateway.iso8583.HandleSwitchTimeout` |

The two `Compute` node ESQL modules and the timeout handler are already
written. Wiring them is Toolkit work.

## The conformance obligation

`Iso8583Library/dfdl/ISO8583.xsd` and
`libs/mfcommon/mfcommon/iso8583/parser.py` are two independent
implementations of the same binary format. They **will** drift unless
something forces them together.

`tests/e2e/test_dfdl_conformance.py` is that forcing function: it builds
messages with the Python codec and asserts the exact byte layout the DFDL
schema declares, BCD packing, odd-length `F` filler, LLVAR digit-vs-byte
counts, bitmap bit positions, and the no-trim rule on DE 52 and DE 64.

If you change padding, length-prefix encoding, or the binary-field list in
`parser.py`, the DFDL schema is now wrong and that test is what will say so.

Two details worth knowing before editing either:

- **LLVAR length is in DIGITS, not bytes.** A 16-digit PAN has `LL = 16` and
  occupies 8 bytes. The single most common ISO 8583 parsing bug.
- **DE 52 and DE 64 must never be trimmed.** An encrypted PIN block can
  coincidentally end in a byte that decodes to whitespace; trimming it
  removes real key material. Rare enough to survive testing and reach
  production, the monolith's README documents finding exactly this via a
  flaky test. The DFDL sets `textTrimKind="none"`; the Python codec carries
  a `binary` flag on `FieldSpec`.

## Building, once entitled

```bash
# 1. Authenticate to the IBM registry with your entitlement key
docker login cp.icr.io --username cp --password <ENTITLEMENT_KEY>

# 2. Compile the BAR (needs the Toolkit, or ibmint in the ACE image)
ibmint package --input-path . --output-bar-file Iso8583Gateway.bar \
               --project Iso8583Gateway --project Iso8583Library

# 3. Build the runtime image
docker build -t microfinance/ace-iso8583-gateway:1.0 .

# 4. Point the adapter at it and stop the stub
#    ISO8583_SOAP_ENDPOINT=http://ace:7800/Iso8583Gateway
```

Then run the existing test suite unchanged. It asserts on the WSDL contract,
not on either implementation, so a green run against ACE is genuine evidence
the swap worked. That is the whole reason the WSDL was written first.

## What will need attention on first contact with a real switch

Honest list, not hypothetical:

1. **DE 90 placeholders.** `BuildReversal` zero-fills the acquiring and
   forwarding institution IDs. The simulator only needs MTI and STAN; a real
   switch will want the genuine values echoed from the original request.
2. **BCD filler position.** This implementation pads odd-length numerics with
   a *trailing* `F`. Some processors pad leading. Confirm against the spec, if it differs, every odd-length field shifts by a nibble.
3. **LLVAR prefix encoding.** BCD here; some processors use ASCII.
4. **STAN scope.** The counter is per integration server. One replica holding
   one connection is correct; scaling out needs either per-replica STAN
   ranges or a shared counter.
5. **MAC (DE 64/128).** Not generated. Most real switches require one, keyed
   by the HSM.
