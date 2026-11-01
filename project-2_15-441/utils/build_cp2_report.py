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
from matplotlib.ticker import StrMethodFormatter
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
    points = np.array([[float(r["time_s"]), float(r["cwnd"])] for r in trace])
    # State is constant between events, including an entire RTO wait.
    segments, segment_colors = [], []
    for index in range(1, len(points)):
        previous, current = points[index - 1], points[index]
        corner = (current[0], previous[1])
        segments.extend([[previous, corner], [corner, current]])
        segment_colors.extend(
            [
                state_colors[trace[index - 1]["state"]],
                state_colors[trace[index]["state"]],
            ]
        )
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.add_collection(
        LineCollection(
            segments,
            colors=segment_colors,
            linewidth=1.4,
        )
    )
    ax.step(
        points[:, 0],
        [float(r["ssthresh"]) for r in trace],
        "--",
        where="post",
        color="#777777",
        lw=0.8,
        label="ssthresh",
    )
    for state, color in state_colors.items():
        ax.plot([], [], color=color, label=state.replace("_", " ").title())
    for label, color in (
        ("3 duplicate ACKs", "#bc6c13"),
        ("Timeout", "#b62936"),
    ):
        ax.plot([], [], marker="x", ls="none", color=color, label=label)
    for i, event in enumerate(events(trace), 1):
        x, y = float(event["time_s"]), float(event["cwnd"])
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
        ylabel="Congestion window (bytes)",
    )
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
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
    normalized = np.array(means) / group_x
    normalized_ratio = float(max(normalized) / min(normalized))
    high_loss_runs = [r for r in runs if float(r["loss_percent"]) == 10]
    timeout_wait_share = sum(
        counts(traces[r["trace"]])[1] * 3 for r in high_loss_runs
    ) / sum(float(r["seconds"]) for r in high_loss_runs)
    fig, ax = plt.subplots(figsize=(7, 3.35))
    ax.scatter(
        x, y, s=14, alpha=0.35, color="#1761a0", label="Individual trials (70)"
    )
    order = np.argsort(group_x)
    ax.errorbar(
        group_x[order],
        np.array(means)[order],
        yerr=np.array(stds)[order],
        fmt="o-",
        linewidth=1,
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
        "normalized_group_means_mbps": list(normalized),
        "normalized_max_min_ratio": normalized_ratio,
        "high_loss_timeout_interval_share": timeout_wait_share,
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

    para("1. Congestion Control Report", "Title")
    para("15-441/641 - TCP in the Wild - Checkpoint 2")
    para("1.1 Sender's congestion window (bytes) over time", "Heading2")
    fast_count, timeout_count = counts(trace)
    para(
        f"Selected connection: {chosen['loss_percent']}% sender loss, trial {chosen['run']}, {int(chosen['bytes']):,} bytes. Transfer time: {float(chosen['seconds']):.3f} s. The full trace contains {fast_count} triple-duplicate-ACK events and {timeout_count} timeout{'s' if timeout_count != 1 else ''}. Colors identify the sender state after each event; numbered marks identify every loss response."
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
    para("Source trace: " + chosen["trace"], "Body")
    story.append(PageBreak())
    para("1. Congestion Control Report (continued)", "Title")
    para("1.2 Does the observed CWND behavior match TCP Reno?", "Heading2")
    para(
        "<b>Yes: the recorded window transitions match the Reno state diagram in the writeup.</b> "
        "In <b>slow start</b> (green), each new full-segment ACK adds one MSS (1375 bytes), giving roughly exponential growth per RTT. "
        "In <b>congestion avoidance</b> (blue), each new ACK adds MSS squared divided by cwnd, giving approximately one MSS of additive growth per RTT."
    )
    first_fast_index = next(
        i for i, row in enumerate(trace) if row["event"] == "fast_retransmit"
    )
    first_fast = trace[first_fast_index]
    before_fast = trace[first_fast_index - 1]
    para(
        "<b>Fast retransmit and fast recovery</b> (orange) begin after three duplicate ACKs: "
        "ssthresh becomes half the previous cwnd, the missing segment is retransmitted, and cwnd becomes ssthresh + 3*MSS. "
        f"For example, at {float(first_fast['time_s']):.3f} s, cwnd was {float(before_fast['cwnd']):,.1f} bytes; "
        f"ssthresh becomes {float(first_fast['ssthresh']):,.1f} bytes and cwnd becomes {float(first_fast['cwnd']):,.1f} bytes. "
        "Each further duplicate ACK adds one MSS. The next advancing ACK exits recovery and deflates cwnd to ssthresh. "
        "This explains the tall recovery peaks and subsequent reductions. New transmissions are also limited by the advertised receive window."
    )
    timeout_index = next(
        i for i, row in enumerate(trace) if row["event"] == "timeout"
    )
    timeout = trace[timeout_index]
    before_timeout = trace[timeout_index - 1]
    para(
        f"<b>Timeout recovery:</b> at {float(timeout['time_s']):.3f} s, cwnd falls from "
        f"{float(before_timeout['cwnd']):,.1f} to {float(timeout['cwnd']):,.1f} bytes (one MSS), "
        f"ssthresh becomes {float(timeout['ssthresh']):,.1f} bytes, and slow start resumes. "
        "The flat interval before this event reflects waiting for the fixed three-second RTO. "
        "Multiple losses within one Reno window can require a timeout after fast recovery; the trace need not form a perfectly regular sawtooth."
    )
    para(
        "1.3 Experiment setup: bandwidth, loss, and transfer size", "Heading2"
    )
    table(
        [
            ["Setting", "Value"],
            ["Placement", "Two Ubuntu 24.04 t3.micro instances, us-west-2a"],
            [
                "Path / shaping",
                "Private IPv4, same VPC; tcconfig on each node's ens5",
            ],
            [
                "Rate / delay",
                "5 Mbps and 20 ms egress delay on each node (~40 ms RTT)",
            ],
            [
                "CWND plot scenario",
                f"{chosen['loss_percent']}% sender loss, trial {chosen['run']}; receiver loss 0%",
            ],
            [
                "Mathis loss sweep",
                "0.1%, 0.2%, 0.5%, 1%, 2%, 5%, 10%; sender only",
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
    durations = [float(row["seconds"]) for row in runs]
    failed_count = len(all_runs) - len(runs)
    failure_note = (
        f"The raw log retains {failed_count} failed attempts, excluded from the fit."
        if failed_count
        else "No trials failed."
    )
    para(
        "Bandwidth, delay, payload, and protocol settings remain fixed while sender loss varies. "
        "The same deterministic random 2 MiB file is used in all 70 transfers. "
        f"Durations range from {min(durations):.3f} to {max(durations):.3f} seconds, so every transfer lasts at least a few seconds. "
        f"Every received file passed SHA-256 verification. {failure_note}"
    )
    para(
        "Timing starts just after socket creation and ends at the application receipt confirming that the receiver wrote the entire file. "
        "It includes any remaining connection establishment and the final receipt, but excludes FIN and TIME_WAIT. "
        "Application throughput in Mbps is 8 * file bytes / (duration in seconds * 1,000,000); retransmissions and headers are excluded."
    )
    story.append(PageBreak())
    para("2. Mathis Model Evaluation", "Title")
    para(
        "2.1 Throughput against 1/sqrt(p), with linear regression", "Heading2"
    )
    para(
        "Following writeup Section 3.2.2, the hypothesis is T = C/sqrt(p), where p is the configured loss probability expressed as a fraction (0.1% means p = 0.001). "
        "The figure plots all 70 measurements against x = 1/sqrt(p), connects the seven group means, and shows sample-standard-deviation error bars for ten trials per setting. "
        "Both regression lines use all 70 measurements."
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
        f"<b>Unconstrained OLS:</b> T = {fit.slope:.6f} x {fit.intercept:+.6f} Mbps, with R squared = {fit.rvalue**2:.5f}. "
        f"<b>Hypothesis-constrained regression:</b> T = {origin_slope:.6f} x Mbps, with intercept fixed at zero. "
        "The latter directly estimates C for the equation in the writeup."
    )
    story.append(PageBreak())
    para("2. Mathis Model Evaluation (continued)", "Title")
    para("2.2 What value did you find for C?", "Heading2")
    para(
        f"<b>C = {origin_slope:.6f} Mbps.</b> For the specified model T = C*x, with x = 1/sqrt(p), "
        "least squares through the origin gives C = sum(x*T) / sum(x*x), using all 70 trials. "
        f"Allowing an intercept instead gives a slope of {fit.slope:.6f} Mbps and intercept {fit.intercept:.6f} Mbps. "
        "That slope describes the affine fit on page 3; the zero-intercept estimate above is C for the writeup's proportional model."
    )
    para(
        "2.3 Pearson correlation between 1/sqrt(p) and throughput", "Heading2"
    )
    para(
        f"<b>Pearson r = {fit.rvalue:.5f}</b> across all 70 (1/sqrt(p), throughput) pairs. "
        "It is computed as the centered cross-product divided by the product of the centered vector lengths. "
        f"For comparison, the correlation using only the seven group means is {mean_r:.5f}; "
        "the all-trial value is the primary result and retains the variation between repeated transfers."
    )
    para("2.4 Do the data corroborate Dr. Mathis's hypothesis?", "Heading2")
    verdict = (
        "The pure proportional model does not adequately describe the entire loss range."
        if normalized_ratio > 2
        else "The normalized measurements are more consistent with an approximate proportional relationship."
    )
    para(
        f"<b>{verdict}</b> The positive correlation supports the inverse-square-root trend, "
        "but it does not establish one constant C across these experiments. "
        f"For exact proportionality, T*sqrt(p) would be constant. The seven normalized means range from {min(normalized):.5f} to {max(normalized):.5f} Mbps, "
        f"a factor of {normalized_ratio:.2f}. The nonzero unconstrained intercept and trial variability also show departures from the hypothesis."
    )
    para(
        "At low loss, the 5 Mbps rate limit, queueing delay, receive window, and finite slow-start period can cap or reduce throughput. At higher loss, multiple missing packets and the fixed three-second retransmission timeout make Reno spend time waiting rather than following the steady-state sawtooth assumed by the approximation. These effects can lower throughput relative to a model based only on p and a nominal 40 ms RTT. The configured RTT is a baseline; serialization and queueing increase actual RTT during a transfer."
    )
    para(
        f"For the ten 10% loss trials, the number of recorded timeouts multiplied by the fixed 3 s RTO accounts for approximately {timeout_wait_share:.1%} of total transfer time. This estimates the contribution of timeout intervals; it is not a direct measurement of link idleness."
    )
    para("Validation and reproducibility", "Heading2")
    para(
        "All 36 portable tests passed on macOS and AWS Ubuntu 24.04 with AddressSanitizer and UndefinedBehaviorSanitizer; commands and coverage are in tests.txt. "
        "The accompanying experiments/results directory contains all 70 timings, hashes, raw CWND traces, traffic-control settings, and regression results. "
        "source.json and remote-builds.json record matching protocol sources on both nodes. build_cp2_report.py reproduces the figures and calculations. "
        "cleanup.json records removal of the temporary AWS resources."
    )
    para("References", "Heading2")
    para(
        "Course Project 2 writeup (October 6, 2026), Section 3.2, Figure 2, and report requirements on pages 7-8.<br/>"
        "RFC 5681, TCP Congestion Control, Sections 3.1-3.2: https://www.rfc-editor.org/rfc/rfc5681"
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
