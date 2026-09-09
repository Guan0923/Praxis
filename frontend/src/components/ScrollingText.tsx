import type { CSSProperties } from "react";
import { useHorizontalOverflow } from "./sidebar/useHorizontalOverflow";

export default function ScrollingText({ text, className = "", focusable = false }: {
  text: string;
  className?: string;
  focusable?: boolean;
}) {
  const { viewportRef, textRef, overflow, measure } = useHorizontalOverflow(text);
  const style = {
    "--thread-scroll-distance": `-${overflow}px`,
    "--thread-scroll-duration": `${Math.min(14, Math.max(4, overflow / 32 + 3))}s`,
  } as CSSProperties;
  return (
    <span
      ref={viewportRef}
      className={`thread-scroll${overflow > 0 ? " thread-scroll--overflow" : ""} ${className}`}
      style={style}
      tabIndex={focusable ? 0 : undefined}
      aria-label={focusable ? text : undefined}
      onMouseEnter={measure}
      onFocus={measure}
    >
      <span ref={textRef} className="thread-scroll-text">{text}</span>
    </span>
  );
}
