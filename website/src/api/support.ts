const API_URL = import.meta.env.VITE_API_URL;

interface SupportFormData {
  firstName: string;
  lastName: string;
  email: string;
  reason: string;
  message: string;
}

export async function submitSupportForm(data: SupportFormData): Promise<void> {
  const response = await fetch(`${API_URL}/website/support`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${response.status})`);
  }
}
