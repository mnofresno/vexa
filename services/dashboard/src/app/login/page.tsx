"use client";

import { Logo } from "@/components/ui/logo";

export default function LoginPage() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-background px-4 text-center">
      <div className="mb-8 flex flex-col items-center gap-3">
        <Logo size="lg" showText={false} />
        <span className="text-lg font-semibold tracking-[-0.02em] text-foreground">Grainbox</span>
      </div>

      <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] text-foreground">
          Sign-in is managed by EasyCasual
        </h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          Grainbox uses bpf-auth for authentication. Open Grainbox through the protected EasyCasual URL and you will be signed in automatically.
        </p>
        <a
          href="https://grainbox.easycasual.app/"
          className="mt-6 inline-flex w-full items-center justify-center rounded-xl bg-primary px-4 py-3 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Continue to Grainbox
        </a>
      </div>
    </main>
  );
}
