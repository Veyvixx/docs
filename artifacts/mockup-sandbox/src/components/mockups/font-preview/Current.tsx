export function Current() {
  return (
    <div style={{ fontFamily: "'Playfair Display', Georgia, serif" }} className="min-h-screen bg-[#FDFAFA] p-8">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=DM+Sans:wght@400;500&display=swap');
      `}</style>

      <div className="flex gap-6 h-full">
        {/* Sidebar */}
        <div className="w-44 flex-shrink-0">
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-3" style={{ fontFamily: "'DM Sans', sans-serif" }}>Guides</p>
          <div className="space-y-1 mb-5">
            <p className="text-xs text-[#C4909E] font-medium flex items-center gap-2" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              <span>🏠</span> Introduction
            </p>
            <p className="text-xs text-gray-500 flex items-center gap-2" style={{ fontFamily: "'DM Sans', sans-serif" }}>
              <span>🚀</span> Quickstart
            </p>
          </div>
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-3" style={{ fontFamily: "'DM Sans', sans-serif" }}>Features</p>
          <div className="space-y-1 mb-5">
            {["Moderation", "Anti-Nuke", "Autoresponders", "Buttons", "Customization", "Event Logging"].map(f => (
              <p key={f} className="text-xs text-gray-500 flex items-center gap-2" style={{ fontFamily: "'DM Sans', sans-serif" }}>
                <span className="text-[#C4909E]">◆</span> {f}
              </p>
            ))}
          </div>
          <p className="text-xs font-semibold uppercase tracking-widest text-gray-400 mb-3" style={{ fontFamily: "'DM Sans', sans-serif" }}>Premium</p>
          <div className="space-y-1">
            <p className="text-xs text-gray-500" style={{ fontFamily: "'DM Sans', sans-serif" }}>⭐ Premium Overview</p>
            <p className="text-xs text-gray-500" style={{ fontFamily: "'DM Sans', sans-serif" }}>🔑 Activation</p>
          </div>
        </div>

        {/* Main content */}
        <div className="flex-1">
          <p className="text-xs text-[#C4909E] font-medium mb-1" style={{ fontFamily: "'DM Sans', sans-serif" }}>Getting Started</p>
          <h1 className="text-4xl font-bold text-gray-900 mb-2" style={{ fontFamily: "'Playfair Display', serif" }}>Introduction</h1>
          <p className="text-sm text-gray-500 mb-5" style={{ fontFamily: "'DM Sans', sans-serif" }}>
            Welcome to the official documentation for Nana — a powerful, beautifully designed Discord bot.
          </p>

          <h2 className="text-2xl font-semibold text-gray-800 mb-2" style={{ fontFamily: "'Playfair Display', serif" }}>What is Nana?</h2>
          <p className="text-sm text-gray-600 mb-5 leading-relaxed" style={{ fontFamily: "'DM Sans', sans-serif" }}>
            Nana is a feature-rich Discord bot built with a soft aesthetic and powerful capabilities. From moderation and anti-nuke protection to fully customizable autoresponders.
          </p>

          <h2 className="text-2xl font-semibold text-gray-800 mb-3" style={{ fontFamily: "'Playfair Display', serif" }}>Feature Guides</h2>
          <div className="grid grid-cols-2 gap-3">
            {[
              { icon: "⚖️", name: "Moderation", desc: "Ban, kick, mute, jail, warn, purge and more." },
              { icon: "🛡️", name: "Anti-Nuke", desc: "Real-time protection against mass destructive actions." },
              { icon: "💬", name: "Autoresponders", desc: "Create trigger-based automatic responses." },
              { icon: "👆", name: "Buttons", desc: "Build interactive link and functional buttons." },
            ].map(card => (
              <div key={card.name} className="border border-gray-200 rounded-lg p-3 bg-white">
                <div className="text-lg mb-1">{card.icon}</div>
                <p className="text-sm font-semibold text-gray-800 mb-1" style={{ fontFamily: "'DM Sans', sans-serif" }}>{card.name}</p>
                <p className="text-xs text-gray-500" style={{ fontFamily: "'DM Sans', sans-serif" }}>{card.desc}</p>
              </div>
            ))}
          </div>

          <div className="mt-4 text-center text-xs text-gray-400 italic" style={{ fontFamily: "'Playfair Display', serif" }}>
            Current: Playfair Display
          </div>
        </div>
      </div>
    </div>
  );
}
