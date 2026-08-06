#!/usr/bin/env python3
"""Probe Ubbink Ubiflux Vigor Modbus input registers to discover undocumented
temperatures (e.g. outdoor-intake and exhaust-out, which the server does NOT
currently read -- it only knows 4036 and 4046).

Read-only: only issues `read_input_registers`, so it cannot change any setting.

Works with the server's own pymodbus 2.5.3 (and with pymodbus 3.x if run
elsewhere). Reads serial settings from the same .env / config the server uses
when those env vars are present, so inside the Docker container you can just run
it with no arguments.

Run it INSIDE the server's container (which has pymodbus + the /dev/ttyUSB0
mapping). The running web service holds the serial port, so stop it first:

    cd ~/homeassistant-vmc-ubbink/ubbink-server
    docker compose stop
    docker compose run --rm --entrypoint python web probe_registers.py
    docker compose up -d

Override anything via flags, e.g. a wider scan:
    docker compose run --rm --entrypoint python web probe_registers.py --start 4000 --end 4110
"""
import argparse
import os
import sys

# pymodbus 2.x (server) vs 3.x (anywhere else): different import paths + ctor.
try:  # pymodbus 3.x
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient
    _PYMODBUS3 = True
except ImportError:  # pymodbus 2.x (==2.5.3, what the server pins)
    from pymodbus.client.sync import ModbusSerialClient, ModbusTcpClient
    _PYMODBUS3 = False

KNOWN = {
    4010: "serial number (BCD, 3 regs)",
    4023: "supply pressure (Pa)",
    4024: "extract pressure (Pa)",
    4031: "supply airflow preset (m3/h)",
    4032: "supply airflow actual (m3/h)",
    4036: "SUPPLY/intake temperature (x0.1 degC)",
    4037: "supply humidity (%)",
    4041: "extract airflow preset (m3/h)",
    4042: "extract airflow actual (m3/h)",
    4046: "EXTRACT/exhaust temperature (x0.1 degC)",
    4047: "extract humidity (%)",
    4050: "bypass status",
    4100: "filter status",
}


def make_serial(port, baud):
    kwargs = dict(port=port, baudrate=baud, stopbits=1, bytesize=8,
                  parity="N", timeout=10)
    if _PYMODBUS3:
        try:
            from pymodbus import FramerType
            return ModbusSerialClient(framer=FramerType.RTU, **kwargs)
        except ImportError:
            from pymodbus.transaction import ModbusRtuFramer
            return ModbusSerialClient(framer=ModbusRtuFramer, **kwargs)
    # pymodbus 2.x uses method="rtu"
    return ModbusSerialClient(method="rtu", **kwargs)


def make_tcp(host, port):
    if _PYMODBUS3:
        try:
            from pymodbus import FramerType
            return ModbusTcpClient(host, port=port, framer=FramerType.RTU, timeout=10)
        except ImportError:
            from pymodbus.transaction import ModbusRtuFramer
            return ModbusTcpClient(host, port=port, framer=ModbusRtuFramer, timeout=10)
    from pymodbus.transaction import ModbusRtuFramer
    return ModbusTcpClient(host, port=port, framer=ModbusRtuFramer, timeout=10)


def read_one(client, addr, slave):
    """Return the register value, or None if the device errors on it."""
    kw = {"slave": slave} if _PYMODBUS3 else {"unit": slave}
    try:
        rr = client.read_input_registers(addr, count=1, **kw)
    except TypeError:
        rr = client.read_input_registers(addr, 1, **kw)
    if rr is None or rr.isError():
        return None
    return rr.registers[0]


def looks_like_temp(value):
    """Plausible air temperature stored as tenths of a degree: 0-60 degC."""
    return value is not None and 0 <= value <= 600


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serial", default=os.getenv("DEVICE_PORT", "/dev/ttyUSB0"),
                   help="serial device (default $DEVICE_PORT or /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=int(os.getenv("BAUDRATE", 19200)),
                   help="serial baud (default $BAUDRATE or 19200)")
    p.add_argument("--host", help="use Modbus-TCP gateway instead of serial")
    p.add_argument("--port", type=int, default=502, help="TCP port (default 502)")
    p.add_argument("--slave", type=int, default=20, help="Modbus slave address (default 20)")
    p.add_argument("--start", type=int, default=4020, help="first register (default 4020)")
    p.add_argument("--end", type=int, default=4055, help="last register inclusive (default 4055)")
    args = p.parse_args()

    client = make_tcp(args.host, args.port) if args.host else make_serial(args.serial, args.baud)
    where = f"{args.host}:{args.port}" if args.host else f"{args.serial}@{args.baud}"
    if not client.connect():
        print(f"ERROR: could not connect ({where}). Is the web container stopped "
              f"so the serial port is free?", file=sys.stderr)
        return 1

    print(f"Connected {where}. Scanning input registers "
          f"{args.start}..{args.end} on slave {args.slave}\n")
    print(f"{'reg':>5}  {'raw':>6}  {'hex':>6}  {'/10':>7}   note")
    print("-" * 70)

    candidates = []
    try:
        for addr in range(args.start, args.end + 1):
            value = read_one(client, addr, args.slave)
            if value is None:
                tail = "  <-- " + KNOWN[addr] if addr in KNOWN else ""
                print(f"{addr:>5}  {'--':>6}  {'--':>6}  {'--':>7}   (no response){tail}")
                continue
            note = KNOWN.get(addr, "")
            flag = ""
            if addr not in KNOWN and looks_like_temp(value):
                flag = "  <== possible TEMPERATURE?"
                candidates.append((addr, value))
            print(f"{addr:>5}  {value:>6}  {value:#06x}  {value/10.0:>7.1f}   {note}{flag}")
    finally:
        client.close()

    print("\nDone.")
    if candidates:
        print("\nUndocumented registers that read like a temperature (value/10 in 0-60 degC):")
        for addr, value in candidates:
            print(f"  reg {addr}: raw {value}  ->  {value/10.0:.1f} degC")
        print("\nTo tell them apart: the outdoor-intake reading tracks the outside\n"
              "temperature; the exhaust reading sits between indoor and outdoor.")
    else:
        print("\nNo extra temperature-looking registers in this range; try a wider\n"
              "scan, e.g. --start 4000 --end 4110.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
