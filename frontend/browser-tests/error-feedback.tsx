import { useState } from "react";
import { createRoot } from "react-dom/client";
import { App, Button } from "antd";
import { ErrorAlerts, showErrorMessage } from "../src/components/errorFeedback";

function Fixture() {
  const { message } = App.useApp();
  const [errors, setErrors] = useState<Error[]>([]);
  return <>
    <Button onClick={() => void showErrorMessage(message, new Error("Toast failure"))}>Show toast</Button>
    <Button onClick={() => setErrors([new Error("Polling failure"), new Error("Polling failure")])}>Poll failure</Button>
    <ErrorAlerts errors={errors} />
  </>;
}
createRoot(document.getElementById("root")!).render(<App><Fixture /></App>);
