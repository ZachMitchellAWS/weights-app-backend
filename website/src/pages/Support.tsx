import { useState } from "react";
import { submitSupportForm } from "../api/support";

const REASONS = [
  "Account issue",
  "Billing / subscription",
  "Bug report",
  "Feature request",
  "Data question",
  "Other",
];

function Support() {
  const [form, setForm] = useState({
    firstName: "",
    lastName: "",
    email: "",
    reason: "",
    message: "",
  });
  const [status, setStatus] = useState<"idle" | "sending" | "sent" | "error">(
    "idle"
  );
  const [errorMessage, setErrorMessage] = useState("");

  const handleChange = (
    e: React.ChangeEvent<
      HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
    >
  ) => {
    setForm({ ...form, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("sending");
    setErrorMessage("");

    try {
      await submitSupportForm(form);
      setStatus("sent");
      setForm({ firstName: "", lastName: "", email: "", reason: "", message: "" });
    } catch (err) {
      setStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Something went wrong. Please try again."
      );
    }
  };

  return (
    <div className="support-page">
      <h1>Contact Support</h1>
      <p className="support-subtitle">
        Have a question or running into an issue? We're here to help.
      </p>

      {status === "sent" ? (
        <div className="success-message">
          Your message has been sent. We'll be in touch soon.
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="support-form">
          <div className="form-row">
            <div className="form-group">
              <label htmlFor="firstName">First Name</label>
              <input
                id="firstName"
                name="firstName"
                type="text"
                required
                value={form.firstName}
                onChange={handleChange}
              />
            </div>
            <div className="form-group">
              <label htmlFor="lastName">Last Name</label>
              <input
                id="lastName"
                name="lastName"
                type="text"
                required
                value={form.lastName}
                onChange={handleChange}
              />
            </div>
          </div>

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
            <label htmlFor="reason">Reason</label>
            <select
              id="reason"
              name="reason"
              required
              value={form.reason}
              onChange={handleChange}
            >
              <option value="" disabled>
                Select a reason...
              </option>
              {REASONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group">
            <label htmlFor="message">Message</label>
            <textarea
              id="message"
              name="message"
              rows={5}
              required
              value={form.message}
              onChange={handleChange}
            />
          </div>

          {status === "error" && (
            <div className="error-message">{errorMessage}</div>
          )}

          <button type="submit" disabled={status === "sending"}>
            {status === "sending" ? "Sending..." : "Send Message"}
          </button>
        </form>
      )}

      <style>{`
        .support-page {
          max-width: 600px;
          margin: 0 auto;
          padding: 2rem 0;
        }
        .support-subtitle {
          color: var(--color-text-secondary);
          margin-bottom: 2rem;
        }
        .support-form {
          display: flex;
          flex-direction: column;
          gap: 1.25rem;
        }
        .form-row {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 1rem;
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
        .form-group select,
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
        .form-group select:focus,
        .form-group textarea:focus {
          border-color: var(--color-accent);
        }
        .form-group textarea {
          resize: vertical;
        }
        .support-form button {
          background-color: var(--color-accent);
          color: #fff;
          border: none;
          border-radius: 8px;
          padding: 0.75rem;
          font-size: 1rem;
          font-weight: 600;
          transition: background-color 0.2s;
        }
        .support-form button:hover:not(:disabled) {
          background-color: var(--color-accent-hover);
        }
        .support-form button:disabled {
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

export default Support;
