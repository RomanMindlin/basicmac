#!/usr/bin/env python3

"""Unicast FUOTA for Basic MAC devices (frag + fwman services) via ChirpStack v4.

The device is class A: ChirpStack sends one queued downlink per uplink. Every
frag/fwman request makes the device answer with an uplink, so once the first
downlink is delivered the queue drains by itself.

Typical flow (update file from `zfwtool.py mkupdate -s <key> [-d <ref.zfw>]`):

  fuotacs.py check --expect-zfw ref.zfw     # device runs the delta reference?
  fuotacs.py send --reboot update.up        # transfer, verify, install
  fuotacs.py cleanup                        # after rejoin: new CRC, free session

Connection settings: --server/--token/--dev-eui or CHIRPSTACK_SERVER,
CHIRPSTACK_API_TOKEN, DEV_EUI. Requires: chirpstack-api, grpcio.
"""

import argparse
import base64
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
    '..', '..', 'basicloader', 'tools', 'fwtool'))

from zfwtool import Update, ZFWArchive  # noqa: E402

FRAG_PORT  = 201
FWMAN_PORT = 203
FRAG_IDX   = 0

# max FRMPayload per DR (no FOpts)
MAXPL = {
    'eu868': [51, 51, 51, 115, 222, 222, 222, 222],
    'us915': [11, 53, 125, 242, 242],
}


# ------------------------------------------------
# Fragmentation (TrackNet coding, same as fuota.c)

def tn_avalanche(x):
    x &= 0xffffffff
    x = (((x >> 16) ^ x) * 0x45d9f3b) & 0xffffffff
    x = (((x >> 16) ^ x) * 0x45d9f3b) & 0xffffffff
    return (x >> 16) ^ x

def checkbits(cid, cct):
    # bit i set -> block i is part of chunk cid
    nw = (cct + 31) // 32
    v = 0
    for i in range(nw):
        v |= tn_avalanche(cid * nw + i) << (32 * i)
    return v & ((1 << cct) - 1)

class Carousel:
    def __init__(self, data, csz, pad=b'*'):
        rem = len(data) % csz
        self.pad = (csz - rem) if rem else 0
        data = data + pad * self.pad
        self.csz = csz
        self.cct = len(data) // csz
        self.blocks = [int.from_bytes(data[i*csz:(i+1)*csz], 'little') for i in range(self.cct)]

    def chunk(self, cid):
        cb = checkbits(cid, self.cct)
        v = 0
        for i in range(self.cct):
            if (cb >> i) & 1:
                v ^= self.blocks[i]
        return v.to_bytes(self.csz, 'little')

class Defrag:
    # gaussian elimination over GF(2), mirrors fuota_process()
    def __init__(self, cct, csz):
        self.cct, self.csz = cct, csz
        self.rows = {}  # pivot -> (checkbits, data)

    def process(self, cid, chunk):
        c, d = checkbits(cid, self.cct), int.from_bytes(chunk, 'little')
        for i in range(self.cct - 1, -1, -1):
            if (c >> i) & 1 and i in self.rows:
                c ^= self.rows[i][0]
                d ^= self.rows[i][1]
        if c:
            self.rows[c.bit_length() - 1] = (c, d)

    def complete(self):
        return len(self.rows) == self.cct

    def unpack(self):
        blocks = [None] * self.cct
        for i in range(self.cct):
            c, d = self.rows[i]
            for j in range(i):
                if (c >> j) & 1:
                    d ^= blocks[j]
            blocks[i] = d
        return b''.join(b.to_bytes(self.csz, 'little') for b in blocks)


# ------------------------------------------------
# Payload builders

def frag_sess_del(idx=FRAG_IDX):
    return struct.pack('BB', 0x03, idx)

def frag_sess_setup(nbfrag, fragsz, pad, idx=FRAG_IDX, algo=1, desc=0):
    assert fragsz % 4 == 0
    return struct.pack('<BBHBBBI', 0x02, idx << 4, nbfrag, fragsz, algo << 3, pad, desc)

def frag_status_req(idx=FRAG_IDX):
    return struct.pack('BB', 0x01, idx << 1)

def data_fragment(cid, chunk, idx=FRAG_IDX):
    return struct.pack('<BH', 0x08, (idx << 14) | cid) + chunk

def fwman_dev_version_req():
    return b'\x01'

def fwman_upgrade_img_req():
    return b'\x04'

def fwman_reboot_now():
    # device registers the update only if the image is valid, then reboots
    return struct.pack('<BI', 0x02, 0)


def build_plan(upfile, csz, extra):
    with open(upfile, 'rb') as f:
        data = f.read()
    fc = Carousel(data, csz)
    nfrag = fc.cct + (extra if extra is not None else max(10, fc.cct // 4))
    if nfrag >= 0x3fff:
        sys.exit('too many fragments, increase --csz')
    dls = [(FRAG_PORT, frag_sess_del() + frag_sess_setup(fc.cct, fc.csz, fc.pad), 'session delete+setup')]
    for cid in range(1, nfrag + 1):
        dls.append((FRAG_PORT, data_fragment(cid, fc.chunk(cid)), f'fragment {cid}'))
    dls.append((FRAG_PORT, frag_status_req(), 'session status'))
    dls.append((FWMAN_PORT, fwman_upgrade_img_req(), 'check image'))

    # self-test: reassemble with the device's algorithm
    ds = Defrag(fc.cct, fc.csz)
    needed = None
    for cid in range(1, nfrag + 1):
        ds.process(cid, fc.chunk(cid))
        if ds.complete():
            needed = cid
            break
    if needed is None or ds.unpack()[:len(data)] != data:
        sys.exit('self-test failed: carousel does not reassemble')
    return data, fc, dls, needed


# ------------------------------------------------
# Uplink decoding

def decode(port, pl):
    out = []
    i = 0
    try:
        while i < len(pl):
            c = pl[i]
            if port == FRAG_PORT:
                if c == 0x00:
                    _, pkg, ver = struct.unpack_from('BBB', pl, i); i += 3
                    out.append(f'frag PackageVersion pkg={pkg} ver={ver}')
                elif c == 0x01:
                    _, ri, miss, st = struct.unpack_from('<BHBB', pl, i); i += 5
                    out.append(f'frag Status idx={ri >> 14} received={ri & 0x3fff} missing={miss} status={st}')
                elif c == 0x02:
                    st = pl[i + 1]; i += 2
                    err = [n for b, n in ((1, 'ENCODING'), (2, 'MEMORY'), (4, 'INDEX'), (8, 'DESCRIPTOR')) if st & b]
                    out.append(f'frag SessionSetup idx={st >> 6} ' + ('OK' if not err else 'ERROR ' + '|'.join(err)))
                elif c == 0x03:
                    st = pl[i + 1]; i += 2
                    out.append(f'frag SessionDelete idx={st & 3} ' + ('no such session' if st & 4 else 'OK'))
                else:
                    out.append(f'frag ? {pl[i:].hex()}'); break
            elif port == FWMAN_PORT:
                if c == 0x00:
                    _, pkg, ver = struct.unpack_from('BBB', pl, i); i += 3
                    out.append(f'fwman PackageVersion pkg={pkg} ver={ver}')
                elif c == 0x01:
                    _, fw, hw = struct.unpack_from('<BII', pl, i); i += 9
                    out.append(f'fwman DevVersion fw_crc=0x{fw:08x} hw=0x{hw:08x}')
                elif c == 0x02:
                    _, t = struct.unpack_from('<BI', pl, i); i += 5
                    out.append(f'fwman RebootTime t={t}')
                elif c == 0x04:
                    st = pl[i + 1]; i += 2
                    names = ['NONE (no image)', 'INVALID (corrupt/bad signature)', 'MISMATCH (incompatible)', 'VALID']
                    s = f'fwman UpgradeImage {names[st] if st < 4 else st}'
                    if st == 3:
                        crc, = struct.unpack_from('<I', pl, i); i += 4
                        s += f' new_crc=0x{crc:08x}'
                    out.append(s)
                else:
                    out.append(f'fwman ? {pl[i:].hex()}'); break
            else:
                out.append(f'app port {port}: {pl.hex()}'); break
    except (struct.error, IndexError):
        out.append(f'truncated: {pl.hex()}')
    return out


# ------------------------------------------------
# ChirpStack

class CS:
    def __init__(self, args):
        import grpc
        from chirpstack_api import api
        self.api = api
        if args.tls:
            ch = grpc.secure_channel(args.server, grpc.ssl_channel_credentials())
        else:
            ch = grpc.insecure_channel(args.server)
        token = args.token or os.environ.get('CHIRPSTACK_API_TOKEN')
        if not token:
            sys.exit('API token required (--token or CHIRPSTACK_API_TOKEN)')
        self.auth = [('authorization', f'Bearer {token}')]
        self.dev = api.DeviceServiceStub(ch)
        self.internal = api.InternalServiceStub(ch)
        self.deveui = args.dev_eui.lower()

    def info(self):
        return self.dev.Get(self.api.GetDeviceRequest(dev_eui=self.deveui), metadata=self.auth)

    def queue(self):
        r = self.dev.GetQueue(self.api.GetDeviceQueueItemsRequest(dev_eui=self.deveui), metadata=self.auth)
        return list(r.result)

    def flush(self):
        self.dev.FlushQueue(self.api.FlushDeviceQueueRequest(dev_eui=self.deveui), metadata=self.auth)

    def enqueue(self, port, data):
        qi = self.api.DeviceQueueItem(dev_eui=self.deveui, confirmed=False, f_port=port, data=data)
        self.dev.Enqueue(self.api.EnqueueDeviceQueueItemRequest(queue_item=qi), metadata=self.auth)

    def watch(self, until=None, region=None, csz=None):
        print('watching device events (Ctrl-C to stop)...')
        req = self.api.StreamDeviceEventsRequest(dev_eui=self.deveui)
        start = time.time() - 5
        try:
            for item in self.internal.StreamDeviceEvents(req, metadata=self.auth):
                # the stream replays recent history first -- skip it
                if item.HasField('time') and item.time.ToSeconds() < start:
                    continue
                if item.description == 'up':
                    ev = json.loads(item.body)
                    port = ev.get('fPort', 0)
                    pl = base64.b64decode(ev.get('data', '') or '')
                    dr = ev.get('dr')
                    lines = decode(port, pl) if port in (FRAG_PORT, FWMAN_PORT) else [f'app port {port}: {pl.hex()}']
                    warn = ''
                    if region in MAXPL and dr is not None and csz is not None and dr < len(MAXPL[region]) \
                            and csz + 3 > MAXPL[region][dr] - 8:
                        warn = f'  [WARNING: DR{dr} max payload {MAXPL[region][dr]} is too small for --csz {csz}]'
                    print(f"UP fcnt={ev.get('fCnt')} dr={dr}: " + '; '.join(lines) + warn)
                    if until and any(until(l) for l in lines):
                        return
                elif item.description == 'log':
                    print(f'LOG {item.body}')
                elif item.description == 'join':
                    print('JOIN')
        except KeyboardInterrupt:
            pass


# ------------------------------------------------
# Commands

def cmd_plan(args):
    data, fc, dls, needed = build_plan(args.up, args.csz, args.extra)
    up = Update.fromfile(args.up)
    print(f'update: {len(data)} bytes, new fw crc 0x{up.fwcrc:08x}')
    print(f'fragments: {fc.cct} x {fc.csz} bytes (pad {fc.pad}), queued {len(dls) - 3} '
          f'(lossless reassembly after {needed}), downlink size {fc.csz + 3} bytes')
    print(f'total downlinks: {len(dls)}')
    if args.verbose:
        for port, pl, what in dls:
            print(f'  port {port}: {pl.hex()}   # {what}')

def cmd_check(args):
    cs = CS(args)
    d = cs.info()
    print(f'device: {d.device.name}  last seen: {d.last_seen_at.ToJsonString() if d.HasField("last_seen_at") else "never"}')
    q = cs.queue()
    if q:
        print(f'WARNING: {len(q)} item(s) already queued (use --flush on send)')
    cs.enqueue(FWMAN_PORT, fwman_dev_version_req())
    print('queued DevVersionReq -- delivered after the next uplink')
    if args.expect_zfw:
        crc = ZFWArchive.fromfile(args.expect_zfw).fw.crc
        print(f'expecting fw_crc=0x{crc:08x}')
    cs.watch(until=lambda l: l.startswith('fwman DevVersion'), region=args.region, csz=args.csz)

def cmd_send(args):
    data, fc, dls, needed = build_plan(args.up, args.csz, args.extra)
    if args.reboot:
        dls.append((FWMAN_PORT, fwman_reboot_now(), 'reboot'))
    cs = CS(args)
    if args.flush:
        cs.flush()
    elif cs.queue() and not args.append:
        sys.exit('device queue is not empty; inspect it in ChirpStack or pass --flush/--append')
    for port, pl, _ in dls:
        cs.enqueue(port, pl)
    print(f'queued {len(dls)} downlinks ({fc.cct} fragments needed)'
          + (', reboot after image check' if args.reboot else ''))
    print('delivery starts with the next uplink from the device')
    cs.watch(until=lambda l: l.startswith('fwman UpgradeImage'), region=args.region, csz=args.csz)
    left = len(cs.queue())
    if left:
        print(f'{left} item(s) still queued')

def cmd_reboot(args):
    cs = CS(args)
    cs.enqueue(FWMAN_PORT, fwman_reboot_now())
    print('queued reboot -- device installs a valid update and rejoins')
    cs.watch()

def cmd_cleanup(args):
    cs = CS(args)
    cs.enqueue(FWMAN_PORT, fwman_dev_version_req())
    cs.enqueue(FRAG_PORT, frag_sess_del())
    print('queued DevVersionReq + session delete')
    cs.watch(until=lambda l: l.startswith('frag SessionDelete'))

def cmd_watch(args):
    CS(args).watch(region=args.region, csz=args.csz)

def cmd_queue(args):
    cs = CS(args)
    print(f'{len(cs.queue())} queued')
    if args.flush:
        cs.flush()
        print('flushed')

def cmd_decode(args):
    pl = bytes.fromhex(args.payload) if all(c in '0123456789abcdefABCDEF' for c in args.payload) \
        else base64.b64decode(args.payload)
    print('\n'.join(decode(args.port, pl)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--server', default=os.environ.get('CHIRPSTACK_SERVER', 'localhost:8080'), help='gRPC host:port')
    p.add_argument('--tls', action='store_true')
    p.add_argument('--token')
    p.add_argument('--dev-eui', default=os.environ.get('DEV_EUI'))
    p.add_argument('--region', default='eu868', choices=['eu868', 'us915'], help='for payload size warnings')
    p.add_argument('--csz', type=int, default=40, help='fragment size, multiple of 4 (default 40)')
    sp = p.add_subparsers(dest='cmd', required=True)

    s = sp.add_parser('plan', help='show fragmentation plan (offline)')
    s.add_argument('up'); s.add_argument('--extra', type=int); s.add_argument('-v', '--verbose', action='store_true')
    s.set_defaults(fn=cmd_plan)
    s = sp.add_parser('check', help='query firmware CRC')
    s.add_argument('--expect-zfw')
    s.set_defaults(fn=cmd_check)
    s = sp.add_parser('send', help='queue update transfer and image check')
    s.add_argument('up'); s.add_argument('--extra', type=int)
    s.add_argument('--flush', action='store_true', help='flush device queue first')
    s.add_argument('--append', action='store_true', help='queue behind existing items')
    s.add_argument('--reboot', action='store_true', help='reboot after image check (installs only if valid)')
    s.set_defaults(fn=cmd_send)
    s = sp.add_parser('reboot', help='reboot device (installs a valid update)')
    s.set_defaults(fn=cmd_reboot)
    s = sp.add_parser('cleanup', help='query firmware CRC and delete frag session')
    s.set_defaults(fn=cmd_cleanup)
    s = sp.add_parser('watch', help='show decoded uplinks')
    s.set_defaults(fn=cmd_watch)
    s = sp.add_parser('queue', help='show (or flush) device queue')
    s.add_argument('--flush', action='store_true')
    s.set_defaults(fn=cmd_queue)
    s = sp.add_parser('decode', help='decode a frag/fwman uplink payload (hex or base64)')
    s.add_argument('port', type=int); s.add_argument('payload')
    s.set_defaults(fn=cmd_decode)

    args = p.parse_args()
    if args.csz % 4:
        sys.exit('--csz must be a multiple of 4')
    if args.cmd not in ('plan', 'decode') and not args.dev_eui:
        sys.exit('--dev-eui required')
    args.fn(args)


if __name__ == '__main__':
    main()
