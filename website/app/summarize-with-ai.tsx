"use client";

import { useEffect, useState } from "react";

const DISMISS_KEY = "ai-summarize-dismissed";

const openAiIcon = <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.073zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.8956zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>;

const claudeIcon = <svg viewBox="0 0 24 24" fill="none" stroke="#D97757" strokeWidth="2.1" strokeLinecap="round" aria-hidden="true"><path d="M12 2.5v4.4M12 17.1v4.4M2.5 12h4.4M17.1 12h4.4M5.3 5.3l3.1 3.1M15.6 15.6l3.1 3.1M18.7 5.3l-3.1 3.1M8.4 15.6l-3.1 3.1"/></svg>;

const perplexityIcon = <svg viewBox="0 0 24 24" fill="none" stroke="#20808D" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" aria-hidden="true"><path d="M12 4v16M12 9L6 3.5v6l6 5.5-6 5.5v-6M12 9l6-5.5v6L12 15l6 5.5v-6"/></svg>;

const providers = [
  { label: "ChatGPT", base: "https://chatgpt.com/?q=", icon: openAiIcon },
  { label: "Claude", base: "https://claude.ai/new?q=", icon: claudeIcon },
  { label: "Perplexity", base: "https://www.perplexity.ai/search?q=", icon: perplexityIcon },
];

export function SummarizeWithAI() {
  const [visible, setVisible] = useState(false);
  const [mobile, setMobile] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 700px)").matches);
  const [expanded, setExpanded] = useState(() => typeof window === "undefined" || !window.matchMedia("(max-width: 700px)").matches);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  useEffect(() => {
    if (localStorage.getItem(DISMISS_KEY)) return;
    if (!/^https?:$/.test(window.location.protocol)) return;
    const media = window.matchMedia("(max-width: 700px)");
    const applyViewport = (event: MediaQueryListEvent) => {
      setMobile(event.matches);
      setExpanded(!event.matches);
    };
    media.addEventListener("change", applyViewport);
    const timer = setTimeout(() => setVisible(true), 1500);
    return () => {
      clearTimeout(timer);
      media.removeEventListener("change", applyViewport);
    };
  }, []);

  if (!visible) return null;

  const prompt = encodeURIComponent(`Tell me about NarratorDB (${origin}). Read their brief at ${origin}/llms.txt and summarize what the product does, how retrieval works, and how to get access.`);
  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, "1");
    setVisible(false);
  };

  return <aside
    className={`ai-pop ${mobile && !expanded ? "is-collapsed" : "is-expanded"}`}
    aria-label="Ask about us in your AI"
    onKeyDown={(event) => {
      if (event.key === "Escape" && mobile && expanded) setExpanded(false);
    }}
  >
    <button
      className="ai-pop-launcher"
      type="button"
      aria-label="Open AI shortcuts"
      aria-expanded={expanded}
      aria-controls="ai-shortcuts-panel"
      onClick={() => setExpanded(true)}
    ><span>AI</span><i>↗</i></button>
    <div className="ai-pop-panel" id="ai-shortcuts-panel">
      {mobile ? <button className="ai-pop-collapse" type="button" aria-label="Collapse AI shortcuts" onClick={() => setExpanded(false)}>←</button> : null}
      <button className="ai-pop-close" type="button" aria-label="Dismiss AI shortcuts" onClick={dismiss}>×</button>
      <div className="ai-pop-row">
        <span className="ai-pop-mouse" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 3l12 10-5.2.6 2.9 5.3-2.5 1.4-2.9-5.3L6 19z"/></svg></span>
        {providers.map((provider) => (
          <a key={provider.label} className="ai-dot" href={provider.base + prompt} target="_blank" rel="noopener" aria-label={`Ask ${provider.label} about NarratorDB`} title={provider.label}>{provider.icon}</a>
        ))}
      </div>
      <span className="ai-pop-label">Ask about us in your AI</span>
    </div>
  </aside>;
}
