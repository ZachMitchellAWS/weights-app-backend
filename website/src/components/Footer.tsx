import { Link } from "react-router-dom";

function Footer() {
  return (
    <footer className="footer">
      <div className="footer-inner">
        <div className="footer-links">
          <Link to="/terms">Terms of Service</Link>
          <Link to="/privacy">Privacy Policy</Link>
          <Link to="/support">Support</Link>
        </div>
        <p className="footer-copy">
          &copy; {new Date().getFullYear()} Lift the Bull. All rights reserved.
        </p>
      </div>
      <style>{`
        .footer {
          border-top: 1px solid var(--color-border);
          background-color: var(--color-surface);
          margin-top: auto;
        }
        .footer-inner {
          max-width: var(--max-width);
          margin: 0 auto;
          padding: 1.5rem;
          text-align: center;
        }
        .footer-links {
          display: flex;
          justify-content: center;
          gap: 1.5rem;
          margin-bottom: 0.75rem;
        }
        .footer-links a {
          color: var(--color-text-secondary);
          font-size: 0.875rem;
        }
        .footer-links a:hover {
          color: var(--color-text);
        }
        .footer-copy {
          color: var(--color-text-secondary);
          font-size: 0.8rem;
        }
      `}</style>
    </footer>
  );
}

export default Footer;
