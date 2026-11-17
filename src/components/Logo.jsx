/** Helix mark: a double helix on a dark tile. Matches public/favicon.svg. */
export function Logo({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="logo">
      <rect x="0.5" y="0.5" width="31" height="31" rx="8" fill="#141419" stroke="var(--line-2)" />
      <g strokeLinecap="round" fill="none">
        <path d="M11.2 6.5h9.6M10.4 16h11.2M11.2 25.5h9.6" stroke="#ececf1" strokeOpacity="0.45" strokeWidth="1.6" />
        <path d="M22 3.5c0 6-12 6.5-12 12.5s12 6.5 12 12.5" stroke="var(--magenta)" strokeWidth="2.6" />
        <path d="M10 3.5c0 6 12 6.5 12 12.5s-12 6.5-12 12.5" stroke="#141419" strokeWidth="5" />
        <path d="M10 3.5c0 6 12 6.5 12 12.5s-12 6.5-12 12.5" stroke="var(--cyan)" strokeWidth="2.6" />
      </g>
    </svg>
  );
}
