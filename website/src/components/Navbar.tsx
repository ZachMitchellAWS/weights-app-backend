import { Link } from "react-router-dom";
import AppStoreButton from "./AppStoreButton";

function Navbar() {
  return (
    <nav className="navbar">
      <div className="navbar-inner">
        <Link to="/" className="navbar-brand">
          Lift the Bull
        </Link>
        <div className="navbar-links">
          <Link to="/">Home</Link>
          <Link to="/support">Support</Link>
          <AppStoreButton />
        </div>
      </div>
      <style>{`
        .navbar {
          border-bottom: 1px solid var(--color-border);
          background-color: var(--color-surface);
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
          padding: 0.75rem 1.5rem;
        }
        .navbar-brand {
          font-size: 1.25rem;
          font-weight: 700;
          color: var(--color-text);
        }
        .navbar-brand:hover {
          color: var(--color-text);
        }
        .navbar-links {
          display: flex;
          align-items: center;
          gap: 1.5rem;
        }
        .navbar-links a {
          color: var(--color-text-secondary);
          font-size: 0.95rem;
        }
        .navbar-links a:hover {
          color: var(--color-text);
        }
      `}</style>
    </nav>
  );
}

export default Navbar;
