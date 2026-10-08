import { Component, type ReactNode } from "react";

/** Shows a friendly reload screen instead of a blank page if anything crashes. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div style={{ minHeight: "100%", display: "grid", placeItems: "center", padding: 24, textAlign: "center" }}>
        <div className="card pad-lg" style={{ maxWidth: 380 }}>
          <h2 style={{ margin: "0 0 8px" }}>Something went wrong</h2>
          <p className="muted" style={{ marginTop: 0 }}>The app may have just been updated. Reloading usually fixes it.</p>
          <button className="btn btn-primary block" onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    );
  }
}
