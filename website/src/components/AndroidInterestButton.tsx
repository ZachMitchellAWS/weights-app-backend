import { Link } from "react-router-dom";

interface AndroidInterestButtonProps {
  size?: "compact" | "hero";
}

// Sizing locks the button to the Apple App Store badge's exact dimensions
// (119.66×40 native, 168×56 hero — derived from the official badge SVG's
// 2.99:1 aspect ratio). Internal padding/icon/text sizes are tuned so the
// "Interested in / Android" lockup fits cleanly inside that fixed width.
const SIZES = {
  compact: {
    width: 120,
    height: 40,
    paddingX: 9,
    gap: 8,
    logoSize: 20,
    ledeSize: 7,
    ledeTracking: 1.0,
    ledeMargin: 2,
    platformSize: 17,
    radius: 8,
  },
  hero: {
    width: 168,
    height: 56,
    paddingX: 12,
    gap: 10,
    logoSize: 28,
    ledeSize: 10,
    ledeTracking: 1.4,
    ledeMargin: 4,
    platformSize: 23,
    radius: 10,
  },
} as const;

function AndroidInterestButton({ size = "compact" }: AndroidInterestButtonProps) {
  const s = SIZES[size];

  return (
    <Link
      to="/android"
      className={`android-interest-button android-interest-button--${size}`}
      aria-label="Register interest in an Android version"
    >
      <svg
        viewBox="0 0 24 24"
        width={s.logoSize}
        height={s.logoSize}
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden
      >
        {/* Material Design "android" icon — recognizable Android robot. */}
        <path
          fill="#3DDC84"
          d="M6 18c0 .55.45 1 1 1h1v3.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V19h2v3.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V19h1c.55 0 1-.45 1-1V8H6v10zM3.5 8C2.67 8 2 8.67 2 9.5v7c0 .83.67 1.5 1.5 1.5S5 17.33 5 16.5v-7C5 8.67 4.33 8 3.5 8zm17 0c-.83 0-1.5.67-1.5 1.5v7c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5v-7c0-.83-.67-1.5-1.5-1.5zm-4.97-5.84l1.3-1.3c.2-.2.2-.51 0-.71-.2-.2-.51-.2-.71 0l-1.48 1.48C13.85 1.23 12.95 1 12 1c-.96 0-1.86.23-2.66.63L7.85.15c-.2-.2-.51-.2-.71 0-.2.2-.2.51 0 .71l1.31 1.31C6.97 3.26 6 5.01 6 7h12c0-1.99-.97-3.75-2.47-4.84zM10 5H9V4h1v1zm5 0h-1V4h1v1z"
        />
      </svg>
      <span className="android-interest-button__text">
        <span className="android-interest-button__lede">Interested in</span>
        <span className="android-interest-button__platform">Android</span>
      </span>
      <style>{`
        .android-interest-button {
          display: inline-flex;
          align-items: center;
          gap: ${s.gap}px;
          width: ${s.width}px;
          height: ${s.height}px;
          padding: 0 ${s.paddingX}px;
          box-sizing: border-box;
          background-color: #000;
          border: 1px solid rgba(255, 255, 255, 0.18);
          border-radius: ${s.radius}px;
          color: #fff !important;
          text-decoration: none;
          transition: background-color 0.2s, border-color 0.2s, transform 0.1s;
        }
        .android-interest-button:hover {
          background-color: #0d0d0d;
          border-color: rgba(255, 255, 255, 0.32);
          color: #fff !important;
        }
        .android-interest-button:active {
          transform: translateY(1px);
        }
        .android-interest-button__text {
          display: flex;
          flex-direction: column;
          align-items: flex-start;
          justify-content: center;
          line-height: 1;
        }
        .android-interest-button__lede {
          font-family: "Inter", sans-serif;
          font-weight: 400;
          font-size: ${s.ledeSize}px;
          letter-spacing: ${s.ledeTracking}px;
          text-transform: uppercase;
          color: rgba(255, 255, 255, 0.75);
          margin-bottom: ${s.ledeMargin}px;
        }
        .android-interest-button__platform {
          font-family: "Inter", sans-serif;
          font-weight: 600;
          font-size: ${s.platformSize}px;
          letter-spacing: 0.3px;
          color: #fff;
          white-space: nowrap;
        }
      `}</style>
    </Link>
  );
}

export default AndroidInterestButton;
