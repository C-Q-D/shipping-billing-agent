import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "remixicon/fonts/remixicon.css";
import "./styles/lumno-tokens.css";
import "./styles/continuous-corners.css";
import "./styles/workbench.css";
import App from "./App.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
