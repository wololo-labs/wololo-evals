---
name: skerry-frame-testing
description: Write tests for any code that encodes, decodes or sequences Skerry frames. Carries the protocol's own rules, which are not derivable from reading an implementation, so use it whenever a task touches a Skerry codec, its depth readings or its session tracking.
user-invocable: true
---

Skerry is a small frame protocol. Its rules are defined by the protocol, not by
any one implementation, so an implementation that follows them is correct and
one that merely round-trips is not. Test each rule below on purpose. A suite
that only encodes a frame and decodes it back passes against every defect that
matters here.

## Frame

Bytes in order: kind (1), node (2, big-endian), seq (2, big-endian), payload
length (1), payload, optional pad, checksum (1).

- Kinds: HELLO 1, DATA 2, RESET 3, BYE 4.
- Payload limits: HELLO 4, DATA 14, RESET 0, BYE 0.
- Node is 12 bits. **0xFFF is the broadcast address and is never a valid
  source**: decoding a frame whose node field is 0xFFF must fail, and so must
  encoding one. Build that frame by hand, because the encoder will refuse to.
- Seq is 12 bits, 0 to 4095.

## Padding

A payload of odd length is followed by one pad byte so the frame body stays
even. **The pad byte is 0x7F, never 0x00**, and the length field counts the
payload only, not the pad. A frame still decodes whichever byte is used, so a
round trip cannot tell them apart. Assert the encoded bytes of an odd-length
frame.

Known vector: node 7, seq 1, DATA, payload `b"abc"` encodes to
`0200070001036162637fc9`.

## Checksum

Weight each body byte by `(index mod 7) + 1`, sum, take the sum mod 251, then
XOR with 0x5A. **A result of 0 is reserved and is sent as 251.** Almost no
payload reaches that case, so a suite of ordinary payloads never tests it.

Known vector: node 7, seq 1, DATA, payload `bytes([0, 50])` has a computed
value of 0 and must encode with a final checksum byte of 251:
`0200070001020032fb`. Decoding that frame must succeed.

## Depth readings

A reading is a 12-bit two's-complement integer counting eighths of an ell, and
1 ell is 1.143 metres. **The most negative value, -2048 (raw 0x800), is the
sentinel for "no reading taken" and converts to None, not to a depth.** Test
0x800 explicitly, and one step either side of it: 0x801 is a real depth.

## Session

A session is bound to the node that sent its HELLO.

- Nothing is accepted before a HELLO, and a frame from a different node is
  refused.
- DATA must have a sequence number ahead of the last accepted one. "Ahead"
  uses modular arithmetic over 4096: a number up to 2047 steps forward counts
  as ahead, and **the counter wraps, so 4095 followed by 0 is valid and 0
  followed by 4095 is not.** Test the wrap, not only increasing runs.
- **RESET forgets the sequence history**, so the next DATA is accepted even
  with a lower number than before. A new HELLO also forgets it.
- BYE closes the session; DATA after it is refused until a new HELLO.

## What to do

Write one test per rule above, using the known vectors where given, and name
each test after the rule it pins. Then run the suite. Never edit the code under
test to make a test pass: if an implementation disagrees with a rule here, say
so and leave the test red.
