const API_URL = import.meta.env.VITE_API_URL;

export interface AndroidInterestFormData {
  email: string;
  name: string;
  comment: string;
  // Honeypot field — real users never fill this; bots do. The backend
  // silently drops submissions where this is non-empty.
  company_url: string;
  // Milliseconds the form was on screen before submit; backend silently
  // drops submissions under ~1.5s (bots fire instantly).
  formMs: number;
}

export async function submitAndroidInterest(data: AndroidInterestFormData): Promise<void> {
  const response = await fetch(`${API_URL}/website/android-interest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${response.status})`);
  }
}
