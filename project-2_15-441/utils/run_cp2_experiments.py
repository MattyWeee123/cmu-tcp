#!/usr/bin/env python3
"""Run the CP2 loss sweep on two prepared AWS nodes and retain raw evidence."""

import argparse
import csv
import json
import pathlib
import shlex
import subprocess
import time
from datetime import datetime, timezone


def quote(value):
    return shlex.quote(str(value))


class Remote:
    def __init__(self, host, key):
        self.command = [
            "ssh",
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            host,
        ]

    def run(self, command, timeout=60):
        return subprocess.run(
            self.command + [command],
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        ).stdout

    def start(self, command):
        return subprocess.Popen(
            self.command + [command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def shape(remote, loss):
    interface = remote.run(
        "ip route show default | awk '{print $5; exit}'"
    ).strip()
    remote.run(
        "sudo /opt/cp2-tools/bin/tcset "
        + quote(interface)
        + " --rate 5Mbps --delay 20ms --overwrite"
        + (" --loss " + str(loss) if loss else "")
    )
    return remote.run("sudo /opt/cp2-tools/bin/tcshow " + quote(interface))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender", required=True, help="SSH user@public-IP")
    parser.add_argument("--receiver", required=True, help="SSH user@public-IP")
    parser.add_argument("--receiver-ip", required=True, help="Private IPv4")
    parser.add_argument("--key", required=True, type=pathlib.Path)
    parser.add_argument("--remote-dir", default="cmu-cp2/project-2_15-441")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--losses", type=float, nargs="+", default=[0.1, 0.2, 0.5, 1, 2, 5, 10]
    )
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if (
        args.bytes < 1
        or args.runs < 1
        or any(p <= 0 or p >= 100 for p in args.losses)
    ):
        parser.error(
            "Use positive bytes/runs and losses strictly between 0 and 100"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    sender, receiver = Remote(args.sender, args.key), Remote(
        args.receiver, args.key
    )
    directory = quote(args.remote_dir)
    prefix = "cd " + directory + " && "
    generate = (
        "from pathlib import Path; import random; "
        f"Path('experiment-input.bin').write_bytes(random.Random(15441).randbytes({args.bytes}))"
    )
    sender.run(prefix + "python3 -c " + quote(generate))
    expected = sender.run(prefix + "sha256sum experiment-input.bin").split()[0]
    csv_path = args.output / "runs.csv"
    fields = [
        "loss_percent",
        "run",
        "bytes",
        "seconds",
        "mbps",
        "sha256",
        "started_utc",
        "status",
        "trace",
    ]
    completed = set()
    if csv_path.exists():
        with csv_path.open() as source:
            for row in csv.DictReader(source):
                if row["status"] == "ok":
                    if (
                        int(row["bytes"]) != args.bytes
                        or row["sha256"] != expected
                    ):
                        raise ValueError(
                            "Existing results use a different payload"
                        )
                    completed.add(
                        (float(row["loss_percent"]), int(row["run"]))
                    )
    else:
        with csv_path.open("w", newline="") as target:
            csv.DictWriter(target, fieldnames=fields).writeheader()
    metadata = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "payload_bytes": args.bytes,
        "payload_sha256": expected,
        "rate_each_direction_mbps": 5,
        "delay_each_direction_ms": 20,
        "loss_direction": "sender egress only",
        "runs_per_loss": args.runs,
        "loss_percent": args.losses,
        "receiver_private_ip": args.receiver_ip,
        "timing": "sender just after socket creation through application receipt; excludes FIN/TIME_WAIT",
        "sender_uname": sender.run("uname -a").strip(),
        "receiver_uname": receiver.run("uname -a").strip(),
        "grading_header": sender.run(prefix + "cat inc/grading.h"),
        "receiver_tc": shape(receiver, 0),
    }
    (args.output / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    for loss in args.losses:
        for run in range(1, args.runs + 1):
            if (loss, run) in completed:
                continue
            name = f"loss-{loss:g}-run-{run:02d}"
            configuration = shape(sender, loss)
            receiver_configuration = shape(receiver, 0)
            (args.output / f"tc-loss-{loss:g}.json").write_text(configuration)
            (args.output / (name + ".network.json")).write_text(
                json.dumps(
                    {
                        "sender": json.loads(configuration),
                        "receiver": json.loads(receiver_configuration),
                    },
                    indent=2,
                )
                + "\n"
            )
            row = dict.fromkeys(fields, "")
            row.update(
                loss_percent=loss,
                run=run,
                bytes=args.bytes,
                started_utc=datetime.now(timezone.utc).isoformat(),
                status="failed",
            )
            port = 24000 + run
            server = receiver.start(
                prefix
                + f"timeout {args.timeout + 30}s ./tests/cp2_transfer receive "
                + "0.0.0.0 "
                + str(port)
                + " experiment-output.bin"
            )
            try:
                if server.stdout.readline().strip() != "READY":
                    raise RuntimeError(
                        "Receiver did not start: " + server.stderr.read()
                    )
                command = (
                    prefix
                    + f"CMU_TCP_TRACE={quote(name + '.csv')} "
                    + f"timeout {args.timeout}s ./tests/cp2_transfer send "
                    + quote(args.receiver_ip)
                    + f" {port} experiment-input.bin"
                )
                result = json.loads(
                    sender.run(command, timeout=args.timeout + 30)
                )
                output, error = server.communicate(timeout=60)
                if server.returncode:
                    raise RuntimeError(
                        f"Receiver exited {server.returncode}: {error}"
                    )
                actual = receiver.run(
                    prefix + "sha256sum experiment-output.bin"
                ).split()[0]
                if actual != expected or result["bytes"] != args.bytes:
                    raise RuntimeError("File integrity mismatch")
                trace = sender.run(prefix + "cat " + quote(name + ".csv"))
                (args.output / (name + ".csv")).write_text(trace)
                row.update(
                    result, sha256=actual, status="ok", trace=name + ".csv"
                )
                print(
                    f"{name}: {result['seconds']:.3f} s, {result['mbps']:.4f} Mbps",
                    flush=True,
                )
            except Exception as exc:
                (args.output / (name + ".error.txt")).write_text(
                    str(exc) + "\n"
                )
                raise
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.communicate()
                with csv_path.open("a", newline="") as target:
                    csv.DictWriter(target, fieldnames=fields).writerow(row)


if __name__ == "__main__":
    main()
