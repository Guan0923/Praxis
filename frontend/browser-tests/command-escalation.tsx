import { useState } from "react";
import { createRoot } from "react-dom/client";
import { App } from "antd";
import DecisionCard from "../src/components/DecisionCard";
import "../src/styles/index.css";

function Fixture() {
  const [choice, setChoice] = useState("");
  return <App><main style={{ maxWidth: 720, padding: 16, margin: "0 auto" }}>
    <DecisionCard request={{
      decision_id: "fixture-escalation", kind: "tool", approval_kind: "sandbox_escalation",
      tool: "run_command", arguments: { cmd: "Get-Content -LiteralPath 'C:/private/example.txt'" },
      cwd: "C:/workspace", details: "Access to the path 'C:/private/example.txt' is denied.",
    }} onSubmit={async (value) => { setChoice(value); }} />
    <output data-testid="choice">{choice}</output>
  </main></App>;
}

createRoot(document.getElementById("root")!).render(<Fixture />);
