"use strict";
/**
 * generate_pptx.js — called by output.py via subprocess.
 * Usage: node generate_pptx.js <data_json_path>
 *
 * Reads chart data from the JSON file, builds a PPTX using pptxgenjs,
 * and writes it to the path specified in data.outputPath.
 */

const PptxGenJS = require("pptxgenjs");
const fs = require("fs");
const path = require("path");

const dataPath = process.argv[2];
if (!dataPath) {
  console.error("Usage: node generate_pptx.js <data.json>");
  process.exit(1);
}

const data = JSON.parse(fs.readFileSync(dataPath, "utf8"));

const {
  outputPath,
  white_bg,
  run_date,
  period_str,
  month_labels,
  country_series,
  abs_dev,
  pct_dev,
  provisional_count,
  sources,
  MAJOR,
  COUNTRY_COLORS,
  COUNTRY_NAMES,
} = data;

const bg    = white_bg ? "FFFFFF" : "0F1823";
const surf  = white_bg ? "F4F7FB" : "151F2E";
const grid  = white_bg ? "DCE8F0" : "1E2E42";
const text  = white_bg ? "1A2A3A" : "C8D8E8";
const muted = "7A96B0";
const AMBER = "F0A020";
const GREEN = "3A9A70";

const pres = new PptxGenJS();
pres.layout = "LAYOUT_WIDE";

const slide = pres.addSlide();

// Background
slide.addShape(pres.ShapeType.rect, {
  x: 0, y: 0, w: "100%", h: "100%",
  fill: { color: bg },
  line: { color: bg },
});

// Title
slide.addText("European Natural Gas Demand Monitor", {
  x: 0.4, y: 0.22, w: 12, h: 0.35,
  fontSize: 16, bold: true, color: text,
  fontFace: "Calibri", margin: 0,
});

const prov_note = provisional_count > 0
  ? `  ·  ${provisional_count} month(s) provisional`
  : "";

slide.addText(
  `Last 12 months  ·  ${period_str}  ·  Data as of ${run_date}${prov_note}`,
  {
    x: 0.4, y: 0.58, w: 12, h: 0.22,
    fontSize: 9, color: muted, fontFace: "Calibri", margin: 0,
  }
);

// Chart 1: stacked column — monthly consumption by country
const displayCountries = [...MAJOR, "Other"];
const chart1Data = displayCountries.map((c) => ({
  name:   COUNTRY_NAMES[c] || c,
  labels: month_labels,
  values: country_series[c] || month_labels.map(() => 0),
}));

slide.addText("Monthly gas consumption by country  (TWh)", {
  x: 0.4, y: 0.9, w: 6, h: 0.25,
  fontSize: 10, bold: true, color: text, fontFace: "Calibri", margin: 0,
});

slide.addChart(pres.ChartType.bar, chart1Data, {
  x: 0.4, y: 1.18, w: 6.1, h: 5.5,
  barDir: "col",
  barGrouping: "stacked",
  chartColors: displayCountries.map((c) => COUNTRY_COLORS[c] || "AAAAAA"),
  showLegend: true,
  legendPos: "b",
  legendFontSize: 8,
  legendColor: text,
  showValue: false,
  catAxisLabelColor: muted,
  valAxisLabelColor: muted,
  valAxisLabelFontSize: 8,
  catAxisLabelFontSize: 8,
  valGridLine: { color: grid, size: 0.5 },
  catGridLine: { style: "none" },
  plotArea: { fill: { color: surf } },
  chartArea: { fill: { color: bg }, border: { color: bg } },
  showTitle: false,
  valAxisMinVal: 0,
});

// Chart 2: deviation combo (bar=TWh, line=%)
slide.addText("Demand deviation vs 2019–21 average  (bars = TWh · line = %)", {
  x: 6.85, y: 0.9, w: 6.1, h: 0.25,
  fontSize: 10, bold: true, color: text, fontFace: "Calibri", margin: 0,
});

const val_min = abs_dev.length > 0 ? Math.min(...abs_dev) * 1.1 : -120;
const pct_min = pct_dev.length > 0 ? Math.min(...pct_dev) * 1.1 : -30;

slide.addChart(
  [
    {
      type: pres.ChartType.bar,
      data: [{ name: "TWh deviation", labels: month_labels, values: abs_dev }],
      options: { chartColors: [AMBER], barDir: "col" },
    },
    {
      type: pres.ChartType.line,
      data: [{ name: "% vs baseline", labels: month_labels, values: pct_dev }],
      options: {
        chartColors: [GREEN],
        lineSize: 2,
        lineSmooth: true,
        secondaryValAxis: true,
        secondaryCatAxis: true,
      },
    },
  ],
  {
    x: 6.85, y: 1.18, w: 6.1, h: 5.5,
    valAxes: [
      {
        showValAxisTitle: false,
        valAxisMinVal: val_min,
        valAxisMaxVal: 0,
        valAxisLabelColor: muted,
        valAxisLabelFontSize: 8,
        valGridLine: { color: grid, size: 0.5 },
      },
      {
        showValAxisTitle: false,
        valAxisMinVal: pct_min,
        valAxisMaxVal: 0,
        valAxisLabelColor: GREEN,
        valAxisLabelFontSize: 8,
        valGridLine: { style: "none" },
      },
    ],
    catAxes: [
      { catAxisLabelColor: muted, catAxisLabelFontSize: 8 },
      { catAxisHide: true },
    ],
    showLegend: true,
    legendPos: "b",
    legendFontSize: 8,
    legendColor: text,
    plotArea: { fill: { color: surf } },
    chartArea: { fill: { color: bg }, border: { color: bg } },
    showTitle: false,
  }
);

// Footnote
const source_str =
  "Sources: " +
  sources.slice(0, 6).sort().join("  ·  ") +
  (provisional_count > 0 ? "  ·  * Provisional months may be revised" : "");

slide.addText(source_str, {
  x: 0.4, y: 7.15, w: 12.5, h: 0.2,
  fontSize: 7, color: muted, fontFace: "Calibri", margin: 0,
});

slide.addNotes(
  `Data as of ${run_date}. ` +
  "Baseline: 2019–2021 monthly average. " +
  "Flow-derived countries (SK, LV, LT, EE, FI, SE): " +
  "consumption estimated from ENTSOG border flow mass balance. " +
  "Other EU: remaining countries aggregated."
);

// Ensure output directory exists
const outDir = path.dirname(outputPath);
if (outDir && !fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

pres.writeFile({ fileName: outputPath })
  .then(() => {
    console.log(`Saved: ${outputPath}`);
  })
  .catch((err) => {
    console.error(`pptxgenjs write failed: ${err.message}`);
    process.exit(1);
  });
