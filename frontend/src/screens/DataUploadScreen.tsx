import { useState } from "react";
import { motion } from "framer-motion";
import { Card, SectionLabel, ProvenanceBadge } from "../shared";
import { forecastApi } from "../api/endpoints";

type Tab = "csv" | "manual";

const PIPELINE_STEPS = [
  { step: "Parsed", description: "CSV file structure verified and rows parsed into timeseries array" },
  { step: "Validated", description: "Date formatting, sequence continuity, and non-negative unit assertions checked" },
  { step: "Features", description: "7-day and 28-day lag features and day-of-week encodings engineered" },
  { step: "Forecast", description: "LASSO v1.4 model inferred 7-day quantile predictions (q50 / q67 / q90)" },
  { step: "Decision", description: "Inventory matching and net position evaluated to produce action recommendation" },
];

export default function DataUploadScreen() {
  const [tab, setTab] = useState<Tab>("csv");
  const [file, setFile] = useState<File | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [result, setResult] = useState<any | null>(null);

  // Pre-fill 30 days for manual entry
  const [manualRows, setManualRows] = useState(() => {
    const today = new Date();
    const rows = [];
    for (let i = 29; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      const iso = d.toISOString().slice(0, 10);
      rows.push({ date: iso, units_issued: Math.floor(22 + Math.random() * 12) });
    }
    return rows;
  });

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true);
    } else if (e.type === "dragleave") {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      const droppedFile = e.dataTransfer.files[0];
      if (droppedFile.name.endsWith(".csv")) {
        setFile(droppedFile);
        setErrorMsg(null);
      } else {
        setErrorMsg("Please upload a valid .csv file.");
      }
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
      setErrorMsg(null);
    }
  };

  const handleCsvSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setErrorMsg("Please select or drop a CSV file.");
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await forecastApi.uploadHistory(formData);
      setResult(res);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || "Failed to process CSV file.";
      setErrorMsg(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleManualSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setErrorMsg(null);
    try {
      const formData = new FormData();
      formData.append("manual_data", JSON.stringify(manualRows));
      const res = await forecastApi.uploadHistory(formData);
      setResult(res);
    } catch (err: any) {
      const msg = err?.response?.data?.detail || err?.message || "Failed to submit manual data.";
      setErrorMsg(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleRowChange = (index: number, val: string) => {
    const num = parseInt(val) || 0;
    setManualRows((prev) => {
      const copy = [...prev];
      copy[index].units_issued = Math.max(0, num);
      return copy;
    });
  };

  return (
    <div className="p-8 max-w-3xl">
      <div className="mb-8">
        <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase tracking-widest mb-2">
          Settings &amp; Onboarding
        </p>
        <h1 className="text-[28px] font-bold text-[#1D1D1F] tracking-tight">
          Data Setup — Demand History
        </h1>
        <p className="text-[14px] text-[#6E6E73] mt-1 font-light">
          Bootstrap or update model forecasts with historical hospital issuing data (date, units_issued).
        </p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-[#F5F5F7] p-1 rounded-full w-fit mb-6">
        <button
          onClick={() => { setTab("csv"); setErrorMsg(null); }}
          className={`px-5 py-1.5 rounded-full text-[13px] font-medium transition-all ${
            tab === "csv" ? "bg-white text-[#1D1D1F] shadow-sm" : "text-[#6E6E73] hover:text-[#1D1D1F]"
          }`}
        >
          Upload CSV
        </button>
        <button
          onClick={() => { setTab("manual"); setErrorMsg(null); }}
          className={`px-5 py-1.5 rounded-full text-[13px] font-medium transition-all ${
            tab === "manual" ? "bg-white text-[#1D1D1F] shadow-sm" : "text-[#6E6E73] hover:text-[#1D1D1F]"
          }`}
        >
          Enter Manually
        </button>
      </div>

      {errorMsg && (
        <div className="mb-5 p-4 bg-[#FFEBEE] border border-[#FFCDD2] text-[#C41230] text-[13px] rounded-[12px] font-medium flex items-center gap-2">
          <span>⚠</span>
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Success Result & Pipeline Step Reveal Animation */}
      {result && (
        <div className="space-y-6 mb-6">
          <Card className="p-6 border-l-4 border-l-[#1A8A2C] bg-[#F4FBF7]">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="w-2.5 h-2.5 rounded-full bg-[#1A8A2C]" />
                <p className="text-[14px] font-bold text-[#1A8A2C]">
                  Forecast &amp; Recommendation Updated Successfully
                </p>
              </div>
              <ProvenanceBadge type="model" />
            </div>
            <p className="text-[13px] text-[#1D1D1F] mb-4">
              {result.message || "Overview and Actions now reflect your newly uploaded demand data."}
            </p>

            <div className="grid grid-cols-2 gap-4 mb-4">
              <div className="bg-white p-4 rounded-[10px] border border-[#E5E5E7]">
                <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase mb-1">New Action</p>
                <p className="text-[20px] font-bold text-[#0071E3]">
                  {result.recommendation?.action} {result.recommendation?.quantity || ""}
                </p>
              </div>
              <div className="bg-white p-4 rounded-[10px] border border-[#E5E5E7]">
                <p className="text-[11px] font-semibold text-[#AEAEB2] uppercase mb-1">History Evaluated</p>
                <p className="text-[20px] font-bold text-[#1D1D1F]">
                  {result.history_days} days
                </p>
              </div>
            </div>

            <p className="text-[12px] text-[#6E6E73]">
              {result.recommendation?.reason_summary}
            </p>
            {result.archived && (
              <p className="text-[12px] text-[#1A7431] mt-3">
                ✓ Source file archived in Supabase Storage: <span className="font-mono">{result.archived.path}</span>
              </p>
            )}
          </Card>

          {/* Sequential 5-Step Pipeline Reveal */}
          <Card className="p-6">
            <SectionLabel>Execution Pipeline Steps</SectionLabel>
            <div className="space-y-3 mt-3">
              {PIPELINE_STEPS.map((stepItem, i) => (
                <motion.div
                  key={stepItem.step}
                  initial={{ opacity: 0, x: -24 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.5, delay: i * 0.3, ease: [0.22, 1, 0.36, 1] }}
                  className="flex items-center gap-3 p-3 bg-[#FAFAFC] border border-[#E5E5E7] rounded-[10px]"
                >
                  <div className="w-6 h-6 rounded-full bg-[#E8F4EB] text-[#1A8A2C] flex items-center justify-center text-[12px] font-bold flex-shrink-0">
                    ✓
                  </div>
                  <div>
                    <span className="text-[13px] font-bold text-[#1D1D1F] mr-2">
                      {stepItem.step}
                    </span>
                    <span className="text-[12px] text-[#6E6E73]">
                      {stepItem.description}
                    </span>
                  </div>
                </motion.div>
              ))}
            </div>
          </Card>

          {/* Mini 7-day Forecast Heatmap */}
          {result.forecast && result.forecast.length > 0 && (
            <Card className="p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <p className="text-[15px] font-bold text-[#1D1D1F]">7-Day Forecast Results</p>
                  <p className="text-[12px] text-[#AEAEB2] mt-0.5">q50 point estimates generated by LASSO v1.4</p>
                </div>
                <ProvenanceBadge type="model" />
              </div>
              <div className="grid grid-cols-7 gap-2">
                {result.forecast.slice(0, 7).map((f: any) => {
                  const intensity = Math.max(0.15, Math.min(1.0, (f.q50 - 10) / 25));
                  return (
                    <div key={f.date} className="flex flex-col items-center">
                      <div
                        className="w-full rounded-[10px] flex items-center justify-center transition-all"
                        style={{
                          height: "64px",
                          backgroundColor: `rgba(0, 113, 227, ${intensity})`,
                        }}
                      >
                        <span className="text-[16px] font-bold text-white">
                          {Math.round(f.q50)}
                        </span>
                      </div>
                      <span className="text-[10px] font-semibold text-[#6E6E73] mt-2 font-mono">
                        {f.date.slice(5)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </Card>
          )}
        </div>
      )}

      {tab === "csv" && (
        <Card className="p-7">
          <SectionLabel>Upload Historical Issuing CSV</SectionLabel>
          <p className="text-[13px] text-[#6E6E73] mb-5 leading-relaxed">
            Format requirements: Two columns named <code className="bg-[#F5F5F7] px-1.5 py-0.5 rounded text-[12px]">date</code> (YYYY-MM-DD) and <code className="bg-[#F5F5F7] px-1.5 py-0.5 rounded text-[12px]">units_issued</code> (integer). Minimum 14 rows required.
          </p>

          <form onSubmit={handleCsvSubmit}>
            <div
              onDragEnter={handleDrag}
              onDragOver={handleDrag}
              onDragLeave={handleDrag}
              onDrop={handleDrop}
              className={`border-2 border-dashed rounded-[16px] p-10 flex flex-col items-center justify-center transition-all ${
                dragActive ? "border-[#0071E3] bg-[#F0F7FF]" : "border-[#E5E5E7] bg-[#FAFAFC]"
              }`}
            >
              <div className="w-12 h-12 rounded-full bg-[#E8F2FD] text-[#0071E3] flex items-center justify-center text-[20px] font-bold mb-3">
                ↑
              </div>
              <p className="text-[14px] font-semibold text-[#1D1D1F] mb-1">
                {file ? file.name : "Drag & drop your CSV file here"}
              </p>
              <p className="text-[12px] text-[#86868B] mb-4">
                {file ? `${(file.size / 1024).toFixed(1)} KB` : "or browse from your computer"}
              </p>

              <label className="px-5 py-2.5 bg-white border border-[#E5E5E7] hover:border-[#0071E3] text-[#1D1D1F] text-[13px] font-medium rounded-full cursor-pointer shadow-sm transition-colors">
                Choose CSV File
                <input
                  type="file"
                  accept=".csv"
                  onChange={handleFileChange}
                  className="hidden"
                />
              </label>
            </div>

            <div className="mt-6 flex justify-end">
              <button
                type="submit"
                disabled={loading || !file}
                className="px-6 py-2.5 bg-[#0071E3] text-white text-[14px] font-medium rounded-full hover:bg-[#0058B0] transition-colors disabled:opacity-50 cursor-pointer"
              >
                {loading ? "Processing CSV & Running Forecast..." : "Upload & Run Forecast"}
              </button>
            </div>
          </form>
        </Card>
      )}

      {tab === "manual" && (
        <Card className="p-7">
          <SectionLabel>Manual Entry — Last 30 Days Issuing</SectionLabel>
          <p className="text-[13px] text-[#6E6E73] mb-5 leading-relaxed">
            Enter daily platelet units issued by your blood bank for each of the last 30 days.
          </p>

          <form onSubmit={handleManualSubmit}>
            <div className="max-h-[360px] overflow-y-auto border border-[#E5E5E7] rounded-[12px] mb-6">
              <table className="w-full text-left text-[13px]">
                <thead className="bg-[#F5F5F7] sticky top-0 border-b border-[#E5E5E7]">
                  <tr>
                    <th className="py-2.5 px-4 font-semibold text-[#1D1D1F]">Date</th>
                    <th className="py-2.5 px-4 font-semibold text-[#1D1D1F]">Units Issued</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F5F5F7]">
                  {manualRows.map((row, i) => (
                    <tr key={row.date} className="hover:bg-[#FAFAFC]">
                      <td className="py-2 px-4 text-[#6E6E73] font-mono">{row.date}</td>
                      <td className="py-2 px-4">
                        <input
                          type="number"
                          min="0"
                          value={row.units_issued}
                          onChange={(e) => handleRowChange(i, e.target.value)}
                          className="w-24 px-3 py-1 border border-[#E5E5E7] rounded-[6px] text-[#1D1D1F] font-semibold focus:border-[#0071E3] outline-none"
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={loading}
                className="px-6 py-2.5 bg-[#0071E3] text-white text-[14px] font-medium rounded-full hover:bg-[#0058B0] transition-colors disabled:opacity-50 cursor-pointer"
              >
                {loading ? "Running Forecast..." : "Submit & Run Forecast"}
              </button>
            </div>
          </form>
        </Card>
      )}
    </div>
  );
}
