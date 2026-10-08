import "@fontsource-variable/plus-jakarta-sans";
import "./styles.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AuthProvider } from "./lib/auth";
import { applyTheme, getThemePref } from "./lib/theme";
import { ToastProvider } from "./lib/toast";

applyTheme(getThemePref());

// When a new version of the app takes over, reload once so the old cached code never talks to the new server.
if ("serviceWorker" in navigator) {
  let reloaded = false;
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (hadController && !reloaded) {
      reloaded = true;
      window.location.reload();
    }
  });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);
