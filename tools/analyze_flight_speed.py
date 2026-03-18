#!/usr/bin/env python3
"""
INAV Blackbox speed analyzer  (INAV 7.x, binary format).
Extracts ground speed from navVel[0/1] and reports statistics.

Usage:  python tools/analyze_flight_speed.py LOG00024.TXT [--debug]
"""

import sys, math, mmap, struct
from pathlib import Path

# ── field table (from header) ────────────────────────────────────────────────
FIELD_NAMES = (
    'loopIteration,time,axisRate[0],axisRate[1],axisRate[2],'
    'axisP[0],axisP[1],axisP[2],axisI[0],axisI[1],axisI[2],'
    'axisF[0],axisF[1],axisF[2],fwAltP,fwAltI,fwAltD,fwAltOut,'
    'fwPosP,fwPosI,fwPosD,fwPosOut,'
    'rcData[0],rcData[1],rcData[2],rcData[3],'
    'rcCommand[0],rcCommand[1],rcCommand[2],rcCommand[3],'
    'vbat,amperage,BaroAlt,AirSpeed,rssi,'
    'gyroADC[0],gyroADC[1],gyroADC[2],'
    'gyroRaw[0],gyroRaw[1],gyroRaw[2],'
    'gyroPeakRoll[0],gyroPeakRoll[1],gyroPeakRoll[2],'
    'gyroPeakPitch[0],gyroPeakPitch[1],gyroPeakPitch[2],'
    'gyroPeakYaw[0],gyroPeakYaw[1],gyroPeakYaw[2],'
    'accSmooth[0],accSmooth[1],accSmooth[2],accVib,'
    'attitude[0],attitude[1],attitude[2],motor[0],'
    'servo[0],servo[1],servo[2],servo[3],servo[4],servo[5],servo[6],servo[7],'
    'servo[8],servo[9],servo[10],servo[11],servo[12],servo[13],servo[14],servo[15],'
    'navState,navFlags,navEPH,navEPV,'
    'navPos[0],navPos[1],navPos[2],'
    'navVel[0],navVel[1],navVel[2],'
    'navTgtVel[0],navTgtVel[1],navTgtVel[2],'
    'navTgtPos[0],navTgtPos[1],navTgtPos[2],'
    'navTgtHdg,navSurf,navAcc[0],navAcc[1],navAcc[2]'
).split(',')

# I-frame encoding per field (from "Field I encoding" header line)
I_ENC = [int(x) for x in (
    '1,1,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0,0, 0,0,0,0,'
    '0,0,0,0, 0,0,0,1, 3,0,0,0,1,'
    '0,0,0, 0,0,0,'
    '1,1,1, 1,1,1, 1,1,1,'
    '0,0,0,1, 0,0,0,1,'
    '0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0,'
    '0,0,0,0, 0,0,0, 0,0,0, 0,0,0, 0,0,0, 0,0,0,0,0'
).replace(' ', '').split(',')]

# I-frame predictor per field (from "Field I predictor" header line)
I_PRED = [int(x) for x in (
    '0,0,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0,0, 0,0,0,0,'
    '0,0,0,0, 0,0,0,4, 9,0,0,0,0,'
    '0,0,0, 0,0,0,'
    '0,0,0, 0,0,0, 0,0,0,'
    '0,0,0,0, 0,0,0,4,'
    '8,8,8,8,8,8,8,8, 8,8,8,8,8,8,8,8,'
    '0,0,0,0, 0,0,0, 0,0,0, 0,0,0, 0,0,0, 0,0,0,0,0'
).replace(' ', '').split(',')]

assert len(FIELD_NAMES) == len(I_ENC) == len(I_PRED), \
    f"Field count mismatch: {len(FIELD_NAMES)} / {len(I_ENC)} / {len(I_PRED)}"

MINTHROTTLE = 1127
SERVO_MID   = 1500

IDX = {n: i for i, n in enumerate(FIELD_NAMES)}
IDX_TIME = IDX['time']
IDX_VN   = IDX['navVel[0]']
IDX_VE   = IDX['navVel[1]']
IDX_VU   = IDX['navVel[2]']
IDX_AS   = IDX['AirSpeed']
IDX_BARO = IDX['BaroAlt']

# ── low-level readers (work on mmap / bytearray / bytes) ─────────────────────

def read_uvb(mm, pos, end):
    """Unsigned LEB128, max 5 bytes."""
    result = shift = 0
    for _ in range(5):
        if pos >= end:
            raise ValueError("EOF")
        b = mm[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("uvb too long")

def read_svb(mm, pos, end):
    """Signed LEB128 (zigzag)."""
    raw, pos = read_uvb(mm, pos, end)
    return (raw >> 1) ^ -(raw & 1), pos

def read_neg14(mm, pos, end):
    """2-byte LE unsigned 14-bit."""
    if pos + 2 > end:
        raise ValueError("EOF")
    v = (mm[pos] | (mm[pos + 1] << 8)) & 0x3FFF
    return v, pos + 2

# ── I-frame parser ────────────────────────────────────────────────────────────

def parse_iframe(mm, pos, end, last_vals):
    """Parse one I-frame; returns (values_list, new_pos)."""
    vals = list(last_vals)
    for i, (enc, pred) in enumerate(zip(I_ENC, I_PRED)):
        if enc == 0:
            v, pos = read_svb(mm, pos, end)
        elif enc == 1:
            v, pos = read_uvb(mm, pos, end)
        elif enc == 3:
            v, pos = read_neg14(mm, pos, end)
        else:
            raise ValueError(f"unexpected enc={enc} field={i}")

        if pred == 0:
            actual = v
        elif pred == 4:
            actual = v + MINTHROTTLE
        elif pred == 8:
            actual = v + SERVO_MID
        elif pred == 9:
            actual = last_vals[i] + v
        else:
            actual = v
        vals[i] = actual

    return vals, pos

# ── header scanner ─────────────────────────────────────────────────────────────

def find_binary_start(mm, size):
    """Return offset of first non-H-header byte."""
    pos = 0
    while pos < size:
        if mm[pos:pos+2] == b'H ':
            while pos < size and mm[pos] not in (13, 10):
                pos += 1
            while pos < size and mm[pos] in (13, 10):
                pos += 1
        else:
            break
    return pos

# ── main ───────────────────────────────────────────────────────────────────────

def analyze(path, debug=False):
    size = Path(path).stat().st_size
    f = open(path, 'rb')
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    binary_start = find_binary_start(mm, size)
    print(f"Binary data starts at byte {binary_start} / {size} ({size//1024//1024} MB)")

    ground_speeds = []
    air_speeds    = []
    altitudes     = []
    timestamps    = []

    last_vals  = [0] * len(FIELD_NAMES)
    last_time  = -1
    good = bad = 0
    pos = binary_start

    while pos < size - 10:
        # fast-scan to next 'I' byte
        next_i = mm.find(b'I', pos)
        if next_i == -1:
            break
        pos = next_i

        try:
            vals, end_pos = parse_iframe(mm, pos + 1, size, last_vals)
        except Exception as e:
            if debug and bad < 5:
                print(f"  parse error at {pos}: {e}")
            pos += 1
            bad += 1
            continue

        t   = vals[IDX_TIME]
        vn  = vals[IDX_VN]
        ve  = vals[IDX_VE]
        asp = vals[IDX_AS]
        alt = vals[IDX_BARO]

        gspd = math.sqrt(vn*vn + ve*ve) * 0.036   # cm/s → km/h
        aspd = asp * 0.036

        time_ok  = (t > last_time) and (0 < t < 7_200_000_000)
        speed_ok = (0 <= gspd <= 300) and (0 <= aspd <= 400)

        if time_ok and speed_ok:
            ground_speeds.append(gspd)
            if aspd > 0.5:
                air_speeds.append(aspd)
            altitudes.append(alt / 100.0)
            timestamps.append(t)
            last_time = t
            last_vals = vals
            good += 1
            pos = end_pos
            if debug and good <= 5:
                print(f"  OK frame #{good} at {next_i}: t={t/1e6:.1f}s "
                      f"gspd={gspd:.1f} km/h asp={aspd:.1f} km/h "
                      f"navVel=({vn},{ve}) baro={alt/100:.0f}m")
        else:
            if debug and bad < 10:
                reason = []
                if not time_ok:  reason.append(f"time {t} <= {last_time} or OOB")
                if not speed_ok: reason.append(f"speed gspd={gspd:.1f} asp={aspd:.1f}")
                print(f"  SKIP at {next_i}: {', '.join(reason)}")
            pos += 1
            bad += 1

    mm.close(); f.close()
    return ground_speeds, air_speeds, altitudes, timestamps, good, bad

def report(gs, asp, alt, ts, good, bad):
    print(f"\nI-frames OK: {good}   skipped: {bad}")

    if not ts:
        print("No data found.")
        return

    dur = (ts[-1] - ts[0]) / 1_000_000
    print(f"Log duration: {dur:.0f} s ({dur/60:.1f} min)\n")

    # --- AirSpeed (pitot) ---
    if asp:
        asp_ms = [v / 3.6 for v in asp]        # km/h -> m/s
        avg    = sum(asp) / len(asp)
        mx     = max(asp)
        mn     = min(asp)
        avg_ms = avg / 3.6

        print("=== AirSpeed (pitot tube) ===")
        print(f"  Samples  : {len(asp)}")
        print(f"  Average  : {avg:.1f} km/h  ({avg_ms:.1f} m/s)")
        print(f"  Maximum  : {mx:.1f} km/h  ({mx/3.6:.1f} m/s)")
        print(f"  Minimum  : {mn:.1f} km/h  ({mn/3.6:.1f} m/s)")

        # std dev
        mean = avg
        sd = math.sqrt(sum((x-mean)**2 for x in asp) / len(asp))
        print(f"  Std dev  : {sd:.1f} km/h")

        print("\n  Distribution (km/h):")
        buckets = [0, 20, 40, 60, 80, 100, 120, 150, 180, 220, 260, 300, 350]
        n = len(asp)
        for lo, hi in zip(buckets, buckets[1:]):
            cnt = sum(1 for s in asp if lo <= s < hi)
            if cnt == 0:
                continue
            pct = cnt * 100 / n
            bar = '#' * int(pct / 2)
            print(f"  {lo:3d}-{hi:3d}: {bar:<25} {pct:5.1f}%  ({cnt})")
        over = sum(1 for s in asp if s >= buckets[-1])
        if over:
            pct = over * 100 / n
            print(f"  >={buckets[-1]:3d}: {'#'*int(pct/2):<25} {pct:5.1f}%  ({over})")
    else:
        print("No AirSpeed data (asp=0 in all frames).")

    # --- navVel ground speed ---
    if gs and max(gs) > 0.5:
        gs_fly = [s for s in gs if s > 5]
        print(f"\n=== Ground Speed (GPS navVel) ===")
        print(f"  Average  : {sum(gs)/len(gs):.1f} km/h")
        if gs_fly:
            print(f"  Avg fly  : {sum(gs_fly)/len(gs_fly):.1f} km/h  ({sum(gs_fly)/len(gs_fly)/3.6:.1f} m/s)")
        print(f"  Maximum  : {max(gs):.1f} km/h  ({max(gs)/3.6:.1f} m/s)")
    else:
        print("\n  navVel=0 throughout -- GPS velocity not populated")
        print("  (normal if drone flew in non-GPS nav mode)")

    if alt and (max(alt) - min(alt)) > 5:
        print(f"\n=== Baro Altitude ===")
        print(f"  Average  : {sum(alt)/len(alt):.1f} m")
        print(f"  Maximum  : {max(alt):.1f} m")
        print(f"  Minimum  : {min(alt):.1f} m")
    print()

if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    args = sys.argv[1:]
    debug = '--debug' in args
    args  = [a for a in args if not a.startswith('-')]
    path  = args[0] if args else 'LOG00024.TXT'

    print(f"Analysing: {path}")
    gs, asp, alt, ts, good, bad = analyze(path, debug=debug)
    report(gs, asp, alt, ts, good, bad)
