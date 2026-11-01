#!/usr/bin/env python3
"""Build the checkpoint 2 report exclusively from saved experiment results."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
import numpy as np
from scipy.stats import linregress, pearsonr
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    Table,
    TableStyle,
    PageBreak,
)


def read_csv(path):
    with path.open() as source:
        return list(csv.DictReader(source))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace", help="Optional selected trace filename")
    args = parser.parse_args()
    metadata = json.loads((args.results / "metadata.json").read_text())
    all_runs = read_csv(args.results / "runs.csv")
    runs = [row for row in all_runs if row["status"] == "ok"]
    losses = [0.1, 0.2, 0.5, 1, 2, 5, 10]
    for loss in losses:
        group = [row for row in runs if float(row["loss_percent"]) == loss]
        if len(group) != 10 or len({row["run"] for row in group}) != 10:
            raise ValueError(
                f"Need exactly ten distinct successful trials at {loss}%"
            )
    if len(runs) != 70:
        raise ValueError(
            "Unexpected trials outside the seven required loss rates"
        )
    traces = {
        row["trace"]: read_csv(args.results / row["trace"]) for row in runs
    }

    def events(trace):
        return [
            r for r in trace if r["event"] in ("fast_retransmit", "timeout")
        ]

    def counts(trace):
        return (
            sum(r["event"] == "fast_retransmit" for r in trace),
            sum(r["event"] == "timeout" for r in trace),
        )

    candidates = [
        row
        for row in runs
        if 1 <= counts(traces[row["trace"]])[1] <= 2
        and 2 <= counts(traces[row["trace"]])[0] <= 12
    ]
    if args.trace:
        chosen = next(row for row in runs if row["trace"] == args.trace)
    elif candidates:
        chosen = min(
            candidates, key=lambda r: abs(counts(traces[r["trace"]])[0] - 4)
        )
    else:
        raise ValueError(
            "No trace has a few fast retransmits and 1-2 timeouts; collect a separate trace"
        )
    trace = traces[chosen["trace"]]
    if not 1 <= counts(trace)[1] <= 2:
        raise ValueError("Selected trace must demonstrate 1-2 timeouts")
    figures = args.results / "figures"
    figures.mkdir(exist_ok=True)
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.dpi": 160,
        }
    )
    state_colors = {
        "slow_start": "#2b7a47",
        "congestion_avoidance": "#1761a0",
        "fast_recovery": "#bc6c13",
    }
    points = np.array(
        [[float(r["time_s"]), float(r["cwnd"]) / 1024] for r in trace]
    )
    segments = np.stack([points[:-1], points[1:]], axis=1)
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.add_collection(
        LineCollection(
            segments,
            colors=[state_colors[r["state"]] for r in trace[1:]],
            linewidth=1.4,
        )
    )
    ax.plot(
        points[:, 0],
        [float(r["ssthresh"]) / 1024 for r in trace],
        "--",
        color="#777777",
        lw=0.8,
        label="ssthresh",
    )
    for state, color in state_colors.items():
        ax.plot([], [], color=color, label=state.replace("_", " ").title())
    for i, event in enumerate(events(trace), 1):
        x, y = float(event["time_s"]), float(event["cwnd"]) / 1024
        color = "#b62936" if event["event"] == "timeout" else "#bc6c13"
        ax.axvline(x, color=color, alpha=0.25, lw=0.8)
        ax.scatter([x], [y], color=color, marker="x", s=28, zorder=3)
        ax.annotate(
            str(i),
            (x, y),
            xytext=(2, 8 + 9 * (i % 2)),
            textcoords="offset points",
            fontsize=8,
            color=color,
        )
    ax.autoscale()
    ax.set_ylim(0, max(points[:, 1]) * 1.22)
    ax.set(
        xlabel="Time since connection creation (s)",
        ylabel="Congestion window (KiB)",
    )
    ax.legend(loc="upper right", fontsize=7, ncol=2)
    ax.grid(alpha=0.16)
    fig.tight_layout()
    fig.savefig(figures / "congestion-window.png")
    plt.close(fig)

    x = np.array(
        [1 / np.sqrt(float(row["loss_percent"]) / 100) for row in runs]
    )
    y = np.array([float(row["mbps"]) for row in runs])
    fit = linregress(x, y)
    origin_slope = float(np.dot(x, y) / np.dot(x, x))
    means, stds = [], []
    for loss in losses:
        values = [
            float(r["mbps"]) for r in runs if float(r["loss_percent"]) == loss
        ]
        means.append(float(np.mean(values)))
        stds.append(float(np.std(values, ddof=1)))
    group_x = 1 / np.sqrt(np.array(losses) / 100)
    mean_r = float(pearsonr(group_x, means).statistic)
    fig, ax = plt.subplots(figsize=(7, 3.35))
    ax.scatter(
        x, y, s=14, alpha=0.35, color="#1761a0", label="Individual trials (70)"
    )
    ax.errorbar(
        group_x,
        means,
        yerr=stds,
        fmt="o",
        color="#102a43",
        capsize=3,
        label="Mean +/- sample SD (10 per rate)",
    )
    line_x = np.linspace(0, max(x) * 1.03, 100)
    ax.plot(
        line_x,
        fit.intercept + fit.slope * line_x,
        color="#bc6c13",
        label="OLS fit with intercept",
    )
    ax.plot(
        line_x,
        origin_slope * line_x,
        "--",
        color="#2b7a47",
        label="Fit through origin",
    )
    ax.set(
        xlabel="1 / sqrt(p), where p is the loss probability",
        ylabel="Application throughput (Mbps)",
    )
    ax.set_ylim(bottom=0)
    ax.grid(alpha=0.16)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figures / "mathis-regression.png")
    plt.close(fig)
    statistics = {
        "ols_slope_mbps": fit.slope,
        "ols_intercept_mbps": fit.intercept,
        "pearson_r_all_trials": fit.rvalue,
        "r_squared": fit.rvalue**2,
        "pearson_r_group_means": mean_r,
        "origin_slope_mbps": origin_slope,
        "mathis_dimensionless_C_at_40ms": origin_slope
        * 1e6
        * 0.040
        / (8 * 1375),
        "selected_trace": chosen["trace"],
    }
    (args.results / "analysis.json").write_text(
        json.dumps(statistics, indent=2) + "\n"
    )

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="Body",
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            spaceAfter=9,
        )
    )
    styles["Title"].textColor = colors.HexColor("#102a43")
    styles["Heading2"].textColor = colors.HexColor("#1761a0")
    story = []

    def para(text, style="Body"):
        story.append(Paragraph(text, styles[style]))

    def table(rows, widths=None):
        item = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor("#e8eff5"),
                    ),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    (
                        "LINEBELOW",
                        (0, 0),
                        (-1, -1),
                        0.3,
                        colors.HexColor("#dbe3eb"),
                    ),
                ]
            )
        )
        story.append(item)
        story.append(Spacer(1, 10))

    para("Checkpoint 2: TCP Reno", "Title")
    para("15-441/641 - TCP in the Wild", "Heading2")
    para(
        "This report evaluates the implemented Reno sender using transfers between two separate AWS EC2 instances. All figures are generated from recorded runs; the accompanying CSV files retain timings, checksums, network settings, and congestion events."
    )
    para("Implementation", "Heading2")
    para(
        "The sender limits outstanding bytes to min(cwnd, rwnd). Slow start increases cwnd by up to one MSS per new data ACK; congestion avoidance adds MSS squared divided by cwnd. Three duplicate ACKs halve the threshold, inflate cwnd by three MSS, and retransmit the oldest missing segment. Further duplicate ACKs inflate cwnd by one MSS. The next advancing ACK exits fast recovery at the threshold. A timeout halves the threshold and resets cwnd to one MSS. This is Reno, without NewReno partial-ACK recovery or selective acknowledgments."
    )
    para(
        "Send buffering includes queued and unacknowledged bytes. Ordered and reordered receive data share MAX_NETWORK_BUFFER. The receive window describes a bounded sequence interval, allowing reordered bytes to occupy existing positions without shrinking the right edge. Zero-window probes and read-triggered window reopening prevent permanent flow-control stalls. SYN/FIN reliability and independent close in each direction remain supported."
    )
    para("Experimental configuration", "Heading2")
    table(
        [
            ["Setting", "Value"],
            ["Placement", "Two Ubuntu 24.04 t3.micro instances, us-west-2a"],
            ["Path", "Private IPv4, same VPC and availability zone"],
            [
                "Rate / delay",
                "5 Mbps and 20 ms egress delay on each node (~40 ms RTT)",
            ],
            [
                "Sender egress loss",
                "0.1%, 0.2%, 0.5%, 1%, 2%, 5%, 10%; receiver loss 0%",
            ],
            [
                "Repetitions / file",
                f"10 at each rate; {metadata['payload_bytes']:,} bytes per transfer",
            ],
            [
                "Protocol parameters",
                "MSS 1375 B; initial cwnd 1375 B; initial ssthresh 88,000 B",
            ],
            [
                "Buffer / timeout",
                "65,535 bytes per direction; fixed RTO 3000 ms",
            ],
        ],
        [1.45 * inch, 5.55 * inch],
    )
    para(
        "Each sender is timed from its first application write until an application receipt confirms that the receiver has written all file bytes. Connection startup and the final receipt are included; FIN and TIME_WAIT are excluded. SHA-256 is compared after every transfer. The random payload and file size are identical across all trials."
    )
    story.append(PageBreak())
    para("Congestion-window behavior", "Title")
    fast_count, timeout_count = counts(trace)
    para(
        f"Selected connection: {chosen['loss_percent']}% sender loss, trial {chosen['run']}, {int(chosen['bytes']):,} bytes. Transfer time: {float(chosen['seconds']):.3f} s. The full trace contains {fast_count} triple-duplicate-ACK events and {timeout_count} timeouts. Colors identify the sender state after each event; numbered marks identify every loss response."
    )
    story.append(
        Image(
            str(figures / "congestion-window.png"),
            width=7 * inch,
            height=3.5 * inch,
        )
    )
    rows = [["Mark", "Time (s)", "Event", "cwnd after (B)", "ssthresh (B)"]]
    for index, event in enumerate(events(trace), 1):
        rows.append(
            [
                str(index),
                event["time_s"],
                (
                    "Timeout"
                    if event["event"] == "timeout"
                    else "3 duplicate ACKs"
                ),
                f"{float(event['cwnd']):,.1f}",
                f"{float(event['ssthresh']):,.1f}",
            ]
        )
    table(rows, [0.5 * inch, 0.9 * inch, 2 * inch, 1.8 * inch, 1.8 * inch])
    para(
        "Slow start grows rapidly as ACKs release more packets. After the threshold is reached, additive growth produces the congestion-avoidance ramp. Triple duplicate ACKs trigger immediate retransmission and temporary window inflation; the next advancing ACK deflates the window to ssthresh. A timeout indicates that ACK feedback was insufficient for fast recovery and restarts from one MSS. Multiple losses in a Reno window can therefore produce additional timeouts."
    )
    para("Source trace: " + chosen["trace"], "Body")
    story.append(PageBreak())
    para("Throughput and the Mathis relationship", "Title")
    para(
        "Let p be the configured independent loss probability, expressed as a fraction. For fixed MSS and RTT, the Mathis approximation predicts throughput proportional to x = 1/sqrt(p). The plot shows all 70 measured application throughputs; error bars are the sample standard deviation within each group of ten."
    )
    story.append(
        Image(
            str(figures / "mathis-regression.png"),
            width=7 * inch,
            height=3.35 * inch,
        )
    )
    table(
        [
            [
                "Loss (%)",
                "Mean (Mbps)",
                "Sample SD",
                "Mean duration (s)",
                "Timeouts (total)",
            ]
        ]
        + [
            [
                f"{loss:g}",
                f"{mean:.5f}",
                f"{std:.5f}",
                f"{np.mean([float(r['seconds']) for r in runs if float(r['loss_percent']) == loss]):.2f}",
                str(
                    sum(
                        counts(traces[r["trace"]])[1]
                        for r in runs
                        if float(r["loss_percent"]) == loss
                    )
                ),
            ]
            for loss, mean, std in zip(losses, means, stds)
        ]
    )
    para(
        f"Ordinary least squares over all trials gives T = {fit.slope:.6f} x {fit.intercept:+.6f} Mbps, with Pearson r = {fit.rvalue:.5f} and R squared = {fit.rvalue**2:.5f}. The correlation of the seven group means is {mean_r:.5f}. A fit constrained through the origin gives T = C/sqrt(p), with C = {origin_slope:.6f} Mbps. Under the dimensionless convention T = C*MSS/(RTT*sqrt(p)), using MSS = 1375 bytes and RTT = 40 ms, C = {statistics['mathis_dimensionless_C_at_40ms']:.5f}."
    )
    story.append(PageBreak())
    para("Interpretation and validation", "Title")
    strength = (
        "strong"
        if mean_r >= 0.9
        else "moderate" if mean_r >= 0.7 else "limited"
    )
    para(
        f"The group means show {strength} positive agreement with the inverse-square-root trend (r = {mean_r:.5f}). This supports the trend to that extent, but it does not establish the Mathis approximation as an exact model of these transfers. The reported intercept and trial variability quantify departures from a pure proportional relationship."
    )
    para(
        "At low loss, the 5 Mbps rate limit, queueing delay, receive window, and finite slow-start period can cap or reduce throughput. At higher loss, multiple missing packets and the fixed three-second retransmission timeout make Reno spend time waiting rather than following the steady-state sawtooth assumed by the approximation. These effects can lower throughput relative to a model based only on p and a nominal 40 ms RTT. The configured RTT is a baseline; serialization and queueing increase actual RTT during a transfer."
    )
    durations = [float(r["seconds"]) for r in runs]
    para(
        f"Transfer durations range from {min(durations):.3f} to {max(durations):.3f} seconds. All 70 files passed the SHA-256 check. {len(all_runs) - len(runs)} failed attempts are retained separately in the raw run log and are excluded from the successful-trial fit. No missing data is interpolated. The experiment uses application goodput, so transport headers, retransmissions, and connection overhead do not count as delivered bytes."
    )
    para("Code validation", "Heading2")
    para(
        "Portable tests use an independent UDP peer to check exact packet behavior, plus actual endpoints for integrity and loss recovery. Coverage includes initial window size, slow start, congestion avoidance, three duplicate ACKs, fast recovery, timeout reduction, flow-control limits, zero-window recovery, overlap/reordering, sequence wrap, handshake, peer isolation, and FIN teardown. AddressSanitizer and UndefinedBehaviorSanitizer runs include changed packet/buffer parameters and 1 MiB bidirectional transfers. A deterministic drop verifies that real endpoints recover before RTO. The experiment driver separately passed a 2 MiB byte-for-byte loopback transfer. Detailed commands and validation results are in tests.txt."
    )
    para("Reproducibility", "Heading2")
    para(
        "run_cp2_experiments.py records the traffic-control configuration, protocol constants, payload checksum, per-run throughput, and raw congestion trace. build_cp2_report.py validates ten successful trials per loss rate, computes both regression forms, and renders this report. runs.csv, metadata.json, analysis.json, and all referenced traces accompany the report. The AWS instances are temporary and are terminated after the results are collected."
    )
    para("References", "Heading2")
    para(
        "Course Project 2 handout, checkpoint 2 requirements and Reno state diagram.<br/>RFC 5681, TCP Congestion Control, sections 3.1-3.2: https://www.rfc-editor.org/rfc/rfc5681<br/>RFC 9293, Transmission Control Protocol, section 3.8.6: https://www.rfc-editor.org/rfc/rfc9293"
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#58697a"))
        canvas.drawString(
            0.75 * inch, 0.45 * inch, "TCP in the Wild | Checkpoint 2"
        )
        canvas.drawRightString(7.75 * inch, 0.45 * inch, str(doc.page))

    document = SimpleDocTemplate(
        str(args.output),
        pagesize=(8.5 * inch, 11 * inch),
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title="Checkpoint 2: TCP Reno",
        author="Matt Wei",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    print(args.output)


if __name__ == "__main__":
    main()
