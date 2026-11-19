import type { components } from "./api/schema";

type HealthStatus = components["schemas"]["HealthStatus"];

const PLACEHOLDER_HEALTH: HealthStatus = {
  status: "ok",
  llm_configured: false,
  llm_model: "deepseek-flash",
  ocr_gateway_url: "https://gateway.pltplt.com/ai/process",
  ocr_direct_mode: true,
  public_relay_configured: false,
};

export default function App() {
  return (
    <main style={{ padding: "24px", fontFamily: "var(--lumno-font-sans)" }}>
      <h1>航运智能账单复核工作台（B00 骨架占位）</h1>
      <p>默认服务状态：{PLACEHOLDER_HEALTH.status}</p>
    </main>
  );
}
