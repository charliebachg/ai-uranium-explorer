import { Component, type ErrorInfo, type ReactNode } from "react";

/** A rendering failure (for example an unbacked value id) shows a visible error instead of a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ai-uranium-explorer]", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-full items-center justify-center bg-ground p-8">
        <div className="glass max-w-lg rounded-2xl p-6">
          <div className="font-semibold text-[16px] text-st-miss">AI Uranium Explorer stopped rendering</div>
          <pre className="mt-3 whitespace-pre-wrap text-[12px] text-ink-2">{this.state.error.message}</pre>
          <button
            type="button"
            className="mt-4 rounded-lg bg-raised px-3 py-1.5 text-[13px] text-ink hover:bg-white/10"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}
