import { useRef, useState } from "react";
import { submitAndroidInterest } from "../api/androidInterest";

function AndroidInterest() {
  const [form, setForm] = useState({
    email: "",
    name: "",
    comment: "",
    company_url: "", // honeypot — must stay empty
  });
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error">(
    "idle"
  );
  const [errorMessage, setErrorMessage] = useState("");

  // Captured at first render so the backend can verify the form was on screen
  // long enough that a human plausibly filled it.
  const mountedAtRef = useRef<number>(Date.now());

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("sending");
    setErrorMessage("");

    try {
      await submitAndroidInterest({
        ...form,
        formMs: Date.now() - mountedAtRef.current,
      });
      setStatus("sent");
      setForm({ email: "", name: "", comment: "", company_url: "" });
    } catch (err) {
      setStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Something went wrong. Please try again."
      );
    }
  };

  return (
    <div className="android-page">
      <div className="android-hero">
        <div className="android-robot">
          <svg
            width="96"
            height="96"
            viewBox="0 0 24 24"
            xmlns="http://www.w3.org/2000/svg"
            aria-hidden
          >
            <path
              fill="#3DDC84"
              d="M6 18c0 .55.45 1 1 1h1v3.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V19h2v3.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5V19h1c.55 0 1-.45 1-1V8H6v10zM3.5 8C2.67 8 2 8.67 2 9.5v7c0 .83.67 1.5 1.5 1.5S5 17.33 5 16.5v-7C5 8.67 4.33 8 3.5 8zm17 0c-.83 0-1.5.67-1.5 1.5v7c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5v-7c0-.83-.67-1.5-1.5-1.5zm-4.97-5.84l1.3-1.3c.2-.2.2-.51 0-.71-.2-.2-.51-.2-.71 0l-1.48 1.48C13.85 1.23 12.95 1 12 1c-.96 0-1.86.23-2.66.63L7.85.15c-.2-.2-.51-.2-.71 0-.2.2-.2.51 0 .71l1.31 1.31C6.97 3.26 6 5.01 6 7h12c0-1.99-.97-3.75-2.47-4.84zM10 5H9V4h1v1zm5 0h-1V4h1v1z"
            />
          </svg>
        </div>
        <h1>Want Lift the Bull on Android?</h1>
        <p className="android-subtitle">
          Lift the Bull is currently only on iOS. Let us know if you'd like an
          Android version.
        </p>
      </div>

      {status === "sent" ? (
        <div className="success-message">
          Thanks — your interest is recorded.
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="android-form">
          <div className="form-group">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              name="email"
              type="email"
              required
              value={form.email}
              onChange={handleChange}
            />
          </div>

          <div className="form-group">
            <label htmlFor="name">Name (optional)</label>
            <input
              id="name"
              name="name"
              type="text"
              value={form.name}
              onChange={handleChange}
            />
          </div>

          <div className="form-group">
            <label htmlFor="comment">
              Anything specific you'd want? (optional)
            </label>
            <textarea
              id="comment"
              name="comment"
              rows={4}
              value={form.comment}
              onChange={handleChange}
            />
          </div>

          {/* Honeypot — invisible to humans, attractive to bots. Off-screen
              rather than `display:none` so smart bots that skip hidden
              fields still see and fill it. */}
          <input
            type="text"
            name="company_url"
            tabIndex={-1}
            autoComplete="off"
            aria-hidden
            value={form.company_url}
            onChange={handleChange}
            style={{
              position: "absolute",
              left: "-9999px",
              width: "1px",
              height: "1px",
              opacity: 0,
              pointerEvents: "none",
            }}
          />

          {status === "error" && (
            <div className="error-message">{errorMessage}</div>
          )}

          <button type="submit" disabled={status === "sending"}>
            {status === "sending" ? "Sending..." : "Register Interest"}
          </button>
        </form>
      )}

      <style>{`
        .android-page {
          max-width: 600px;
          margin: 0 auto;
          padding: 2rem 0;
        }
        .android-hero {
          display: flex;
          flex-direction: column;
          align-items: center;
          text-align: center;
          margin-bottom: 2.25rem;
        }
        .android-robot {
          display: inline-flex;
          margin-bottom: 1.25rem;
        }
        .android-subtitle {
          color: var(--color-text-secondary);
          max-width: 480px;
          margin-bottom: 0;
        }
        .android-form {
          display: flex;
          flex-direction: column;
          gap: 1.25rem;
        }
        .form-group {
          display: flex;
          flex-direction: column;
          gap: 0.35rem;
        }
        .form-group label {
          font-size: 0.875rem;
          font-weight: 500;
          color: var(--color-text-secondary);
        }
        .form-group input,
        .form-group textarea {
          background-color: var(--color-surface);
          border: 1px solid var(--color-border);
          border-radius: 8px;
          padding: 0.65rem 0.75rem;
          color: var(--color-text);
          outline: none;
          transition: border-color 0.2s;
        }
        .form-group input:focus,
        .form-group textarea:focus {
          border-color: var(--color-accent);
        }
        .form-group textarea {
          resize: vertical;
        }
        .android-form button {
          background-color: var(--color-accent);
          color: var(--color-accent-ink);
          border: none;
          border-radius: var(--radius-button);
          padding: 0.8rem;
          font-size: 1rem;
          font-weight: 600;
          letter-spacing: 0.5px;
          transition: background-color 0.2s;
        }
        .android-form button:hover:not(:disabled) {
          background-color: var(--color-accent-hover);
          color: var(--color-accent-ink);
        }
        .android-form button:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
        .success-message {
          background-color: var(--color-surface);
          border: 1px solid var(--color-success);
          border-radius: 8px;
          padding: 1.25rem;
          color: var(--color-success);
          text-align: center;
        }
        .error-message {
          color: var(--color-error);
          font-size: 0.875rem;
        }
      `}</style>
    </div>
  );
}

export default AndroidInterest;
