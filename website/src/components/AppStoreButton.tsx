interface AppStoreButtonProps {
  size?: "compact" | "hero";
}

// Native Apple badge aspect: 119.66407 × 40 ≈ 2.99:1.
// We size by height; width follows the aspect ratio so the artwork is
// never distorted (Apple's brand guidelines require it).
const HEIGHTS = {
  compact: 40,
  hero: 56,
} as const;

function AppStoreButton({ size = "compact" }: AppStoreButtonProps) {
  const height = HEIGHTS[size];

  return (
    <a
      href="https://apps.apple.com/us/app/lift-the-bull/id6759113833"
      className={`appstore-button appstore-button--${size}`}
      target="_blank"
      rel="noopener noreferrer"
      aria-label="Download on the App Store"
    >
      <img
        src="/app-store-badge.svg"
        alt="Download on the App Store"
        height={height}
        style={{ height: `${height}px`, width: "auto", display: "block" }}
      />
      <style>{`
        .appstore-button {
          display: inline-flex;
          align-items: center;
          transition: opacity 0.2s, transform 0.1s;
        }
        .appstore-button:hover {
          opacity: 0.85;
        }
        .appstore-button:active {
          transform: translateY(1px);
        }
      `}</style>
    </a>
  );
}

export default AppStoreButton;
