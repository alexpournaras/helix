export function Card({ title, description, actions, children, className = "", flush = false }) {
  return (
    <section className={"card " + className}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {description && <p className="card-desc">{description}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? "" : "card-body"}>{children}</div>
    </section>
  );
}

export function PageIntro({ children }) {
  return <p className="page-intro">{children}</p>;
}

export function OfflineNotice({ onRetry }) {
  return (
    <div className="notice" role="alert">
      <span>The backend isn’t responding. Start it with <code>npm run dev</code>, then try again.</span>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Empty({ icon: Icon, title, children, action }) {
  return (
    <div className="empty">
      {Icon && <div className="empty-icon"><Icon size={18} strokeWidth={1.75} /></div>}
      <div className="empty-title">{title}</div>
      {children && <p className="empty-text">{children}</p>}
      {action}
    </div>
  );
}
