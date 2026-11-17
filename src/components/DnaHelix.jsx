import { useMemo } from "react";
import "../styles/helix.scss";

const BASES = ["A", "T", "C", "G"];
const DEFAULT_RUNGS = 55;
const DEFAULT_MUTATIONS = [3, 7, 11, 24, 38];
const rand = (min, max) => min + Math.random() * (max - min);

/**
 * Animated DNA double helix. Rungs listed in `mutations` render in the mutation colour.
 * Absolutely positioned: place it inside a relatively positioned, overflow-hidden box.
 */
export function DnaHelix({ density = 1, mutations = DEFAULT_MUTATIONS, still = false }) {
  const rungCount = Math.max(34, Math.round(DEFAULT_RUNGS * density));
  const mutationSet = useMemo(() => new Set(mutations), [mutations]);

  const particles = useMemo(() => {
    const count = Math.max(18, Math.round(34 * density));
    return Array.from({ length: count }, (_, id) => ({
      id,
      letter: BASES[Math.floor(Math.random() * BASES.length)],
      style: {
        "--start-x": `${rand(58, 97).toFixed(2)}%`,
        "--start-y": `${rand(-6, 96).toFixed(2)}%`,
        "--fly-x": `${rand(-460, -90).toFixed(1)}px`,
        "--fly-y": `${rand(-170, 210).toFixed(1)}px`,
        "--delay": `${rand(-7.5, 0).toFixed(2)}s`,
        "--duration": `${rand(4.2, 7.2).toFixed(2)}s`,
        "--spin": `${rand(-280, 280).toFixed(1)}deg`,
        "--scale": rand(0.72, 1.32).toFixed(2),
      },
    }));
  }, [density]);

  return (
    <div className={"dna-field" + (still ? " is-still" : "")} aria-hidden="true">
      <div className="base-stream">
        {particles.map((p) => (
          <span key={p.id} className="base-particle" style={p.style}>{p.letter}</span>
        ))}
      </div>

      <div className="helix-container">
        <div className="dna" style={{ "--count": rungCount }}>
          {Array.from({ length: rungCount }, (_, i) => (
            <div key={i} className={"item" + (mutationSet.has(i) ? " is-mut" : "")} style={{ "--i": i }}>
              <div />
              <div />
              <div />
            </div>
          ))}
        </div>
      </div>

      <svg className="helix-filter" xmlns="http://www.w3.org/2000/svg">
        <filter id="helixBlurFilter">
          <feGaussianBlur stdDeviation="4" />
          <feColorMatrix type="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 19 -9" />
        </filter>
      </svg>
    </div>
  );
}
