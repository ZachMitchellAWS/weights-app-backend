import { Link } from "react-router-dom";
import { BullIcon } from "./BullIcon";

function Navbar() {
  return (
    <nav className="navbar">
      <div className="navbar-inner">
        <Link to="/" className="navbar-brand">
          <BullIcon size={28} color="#FFC850" />
          <span className="navbar-brand__text">Lift the Bull</span>
        </Link>
        <div className="navbar-links">
          <Link to="/">Home</Link>
          <Link to="/support">Support</Link>
        </div>
      </div>
      <style>{`
        .navbar {
          border-bottom: 1px solid var(--color-border);
          background-color: rgba(0, 0, 0, 0.78);
          backdrop-filter: blur(20px);
          -webkit-backdrop-filter: blur(20px);
          position: sticky;
          top: 0;
          z-index: 100;
        }
        .navbar-inner {
          max-width: var(--max-width);
          margin: 0 auto;
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0.85rem 1.5rem;
          gap: 1rem;
        }
        .navbar-brand {
          display: inline-flex;
          align-items: center;
          gap: 10px;
          color: var(--color-text);
          min-width: 0;
        }
        .navbar-brand:hover {
          color: var(--color-text);
        }
        .navbar-brand svg {
          display: block;
          flex-shrink: 0;
        }
        .navbar-brand__text {
          font-family: var(--font-display);
          font-size: 1.5rem;
          letter-spacing: 3px;
          text-transform: uppercase;
          line-height: 1;
          white-space: nowrap;
        }
        .navbar-links {
          display: flex;
          align-items: center;
          gap: 1.5rem;
          flex-shrink: 0;
        }
        .navbar-links a {
          color: var(--color-text-secondary);
          font-size: 0.95rem;
          transition: color 0.2s;
        }
        .navbar-links a:hover {
          color: var(--color-accent);
        }
        @media (max-width: 600px) {
          .navbar-inner {
            padding: 0.7rem 1rem;
          }
          .navbar-brand svg {
            width: 38px;
            height: auto;
          }
          .navbar-brand {
            gap: 12px;
          }
          .navbar-brand__text {
            font-size: 1.35rem;
            letter-spacing: 2px;
          }
          .navbar-links {
            gap: 1.1rem;
          }
          .navbar-links a {
            font-size: 0.9rem;
          }
        }
      `}</style>
    </nav>
  );
}

export default Navbar;
