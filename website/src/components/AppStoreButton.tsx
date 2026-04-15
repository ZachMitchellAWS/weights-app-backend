function AppStoreButton() {
  return (
    <a
      href="https://apps.apple.com/us/app/lift-the-bull/id6759113833"
      className="appstore-button"
      target="_blank"
      rel="noopener noreferrer"
    >
      Download
      <style>{`
        .appstore-button {
          display: inline-block;
          padding: 0.5rem 1rem;
          background-color: var(--color-accent);
          color: #fff !important;
          border-radius: 8px;
          font-size: 0.875rem;
          font-weight: 600;
          transition: background-color 0.2s;
        }
        .appstore-button:hover {
          background-color: var(--color-accent-hover);
          color: #fff !important;
        }
      `}</style>
    </a>
  );
}

export default AppStoreButton;
