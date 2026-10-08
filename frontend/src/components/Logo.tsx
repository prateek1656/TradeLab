export function LogoMark({ size = 34 }: { size?: number }) {
  return (
    <svg className="logo" width={size} height={size} viewBox="0 0 64 64" aria-hidden>
      <defs>
        <linearGradient id="lg-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#0f3524" />
          <stop offset="1" stopColor="#03100a" />
        </linearGradient>
        <linearGradient id="lg-c" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4dffb8" />
          <stop offset="1" stopColor="#0fa968" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="16" fill="url(#lg-bg)" />
      <g stroke="#3cf5a6" strokeWidth="2.2" strokeLinecap="round">
        <line x1="18" y1="31" x2="18" y2="51" />
        <line x1="32" y1="20" x2="32" y2="45" />
        <line x1="46" y1="9" x2="46" y2="36" />
      </g>
      <rect x="13.5" y="35" width="9" height="12" rx="2.5" fill="url(#lg-c)" />
      <rect x="27.5" y="24" width="9" height="16" rx="2.5" fill="url(#lg-c)" />
      <rect x="41.5" y="13" width="9" height="18" rx="2.5" fill="url(#lg-c)" />
    </svg>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <LogoMark />
      <span>
        Trade<span className="lab">Lab</span>
      </span>
    </div>
  );
}
