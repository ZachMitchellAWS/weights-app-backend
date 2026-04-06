import AppStoreButton from "../components/AppStoreButton";

function Home() {
  return (
    <div className="home">
      <section className="hero">
        <h1>Lift the Bull</h1>
        <p className="tagline">
          Strength training, simplified. Track your lifts, plan your sets, and
          watch your progress grow.
        </p>
        <AppStoreButton />
      </section>

      <section className="features">
        <div className="feature-card">
          <h3>Track Every Set</h3>
          <p>
            Log exercises, reps, and weight with a fast, intuitive interface
            built for the gym floor.
          </p>
        </div>
        <div className="feature-card">
          <h3>Smart Set Plans</h3>
          <p>
            Create reusable set plan templates so you always know what's next
            when you step up to the bar.
          </p>
        </div>
        <div className="feature-card">
          <h3>Progress Insights</h3>
          <p>
            See estimated 1RM trends and AI-powered weekly training summaries to
            keep you on track.
          </p>
        </div>
      </section>

      <style>{`
        .hero {
          text-align: center;
          padding: 4rem 0 3rem;
        }
        .hero h1 {
          font-size: 3rem;
          margin-bottom: 1rem;
        }
        .tagline {
          font-size: 1.2rem;
          color: var(--color-text-secondary);
          max-width: 540px;
          margin: 0 auto 2rem;
        }
        .features {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 1.5rem;
          padding: 2rem 0;
        }
        .feature-card {
          background-color: var(--color-surface);
          border: 1px solid var(--color-border);
          border-radius: 12px;
          padding: 1.5rem;
        }
        .feature-card h3 {
          margin-bottom: 0.5rem;
        }
        .feature-card p {
          color: var(--color-text-secondary);
          font-size: 0.95rem;
        }
      `}</style>
    </div>
  );
}

export default Home;
