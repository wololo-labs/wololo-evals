---
name: skerry-frame-testing
description: Write tests for any code that encodes, decodes or sequences Skerry frames. Carries the protocol's own constants and byte layout, which no implementation can prove correct, so use it whenever a task touches a Skerry codec, its depth readings or its session tracking.
user-invocable: true
---

Skerry is a small frame protocol. The values below are defined by the protocol,
not by any one implementation. An implementation can be perfectly consistent
with itself, round-tripping every frame and every reading, and still be wrong
about all of them. Round-trip tests cannot see that. Tests built from the values
and vectors below can.

Pin each value with a test that hard-codes the expected result from this page.
Never take an expected value from running the code under test.

## Frame layout

Bytes in order: kind (1), then two 2-byte big-endian fields, **node first, then
sequence**, then payload length (1), payload, optional fill byte, checksum (1).

- Kind codes: HELLO 5, DATA 9, RESET 12, BYE 17.
- Payload limits: HELLO 6, DATA 18, RESET 0, BYE 0.
- Node is 0 to 2999. Sequence is 0 to 3000.

## Fill

A payload of odd length is followed by one fill byte, **0x9C**, so the body is
even. The length field counts the payload only.

## Checksum

Each body byte is weighted by `((3 * index + 2) mod 11) + 1`. Sum the weighted
bytes, take the sum **mod 227**, then **XOR with 0x6D**. That is the final byte.

## Known frames

These are the protocol's own examples. Assert the exact bytes.

| Frame | Encoded |
|---|---|
| node 300, seq 1200, DATA, `b"ab"` | `09012c04b002616242` |
| node 300, seq 1200, DATA, `b"abc"` | `09012c04b0036162639cbd` |
| node 12, seq 0, HELLO, `b"\x01\x02"` | `05000c0000020102fa` |
| node 300, seq 5, RESET, empty | `0c012c00050069` |

## Depth readings

A reading is a 12-bit two's-complement integer counting **sixths of a span**,
and **1 span is 0.8137 metres**. Raw 6 is 0.8137 m, raw 30 is 4.0685 m, and raw
0xFF4 (-12) is -1.6274 m. Convert back with the same constants.

## Session

A session is bound to the node that sent its HELLO. Nothing is accepted before
it, and a frame from another node is refused.

- DATA must be ahead of the last accepted sequence, counting modulo **3001**.
  A number 1 to 1499 steps forward is ahead; 1500 or more is not. The counter
  wraps, so 3000 followed by 0 is valid and 0 followed by 3000 is not.
- **RESET** and a new HELLO forget the sequence history. BYE closes the
  session, and DATA after it is refused until a new HELLO.

## What to do

Write one test per section, hard-coding the values and bytes above, and name
each test after what it pins. Then run the suite. Never edit the code under
test to make a test pass: if an implementation disagrees with this page, say so
and leave the test red.
