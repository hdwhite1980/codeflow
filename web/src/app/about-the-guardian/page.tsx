import Link from "next/link";
import { ArrowLeft, Brain, Code2, Eye, Shield } from "lucide-react";

import { Button } from "@/components/ui/button";

export const metadata = {
  title: "About the Guardian — Code Flow",
  description:
    "How Code Flow remembers your project: file and symbol-level " +
    "semantic indexing, risk reasoning, and change tracking.",
};

/**
 * Customer-facing explainer for the guardian system.
 *
 * Linked from the Project Memory panel header. Aim is to answer the
 * three questions a new user asks:
 *   1. What is the guardian and what does it do for me?
 *   2. Why does indexing take a few minutes?
 *   3. What can I ask it?
 *
 * Plain language. No marketing fluff. Short concrete examples.
 */
export default function AboutTheGuardianPage() {
  return (
    <div className="max-w-3xl mx-auto px-6 py-8 space-y-8">
      <div className="flex items-center gap-3">
        <Link href="/">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="h-4 w-4 mr-1" />
            Back
          </Button>
        </Link>
      </div>

      <header className="space-y-2">
        <div className="flex items-center gap-3">
          <Brain className="h-8 w-8 text-violet-400" />
          <h1 className="text-3xl font-bold tracking-tight">
            About the Guardian
          </h1>
        </div>
        <p className="text-muted-foreground">
          Code Flow's memory system. Indexes every file and function in
          your project so the AI can answer questions like &quot;what
          calls this&quot; and &quot;what breaks if I change
          that&quot; against real understanding, not guesses.
        </p>
      </header>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <Eye className="h-5 w-5 text-violet-400" />
          What it does
        </h2>
        <p className="leading-relaxed">
          After every build and iteration, the guardian reads each file
          in your project and produces a structured summary covering:
        </p>
        <ul className="space-y-2 ml-4 list-disc list-inside">
          <li>
            <strong>Purpose</strong> — one sentence describing what the
            file is for
          </li>
          <li>
            <strong>Touches</strong> — what state, services, or data
            the file reads or writes
          </li>
          <li>
            <strong>Assumes</strong> — preconditions and invariants the
            code relies on
          </li>
          <li>
            <strong>Failure modes</strong> — how the file can break
          </li>
          <li>
            <strong>Risk notes</strong> — concrete concerns a code
            reviewer would flag
          </li>
        </ul>
        <p className="leading-relaxed">
          Then it does the same thing again at the function and class
          level — every function, method, class, and constant in your
          project gets its own summary. Click any file in the Project
          Memory panel and the &quot;Show symbols&quot; toggle reveals
          per-function detail.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <Code2 className="h-5 w-5 text-emerald-400" />
          What you can ask it
        </h2>
        <p className="leading-relaxed">
          Two surfaces use guardian memory:
        </p>
        <div className="space-y-3 ml-4">
          <div>
            <h3 className="font-medium">Risk analyzer</h3>
            <p className="text-sm text-muted-foreground mt-1">
              Top-left of every project page. Ask things like
              &quot;what breaks if I drop the engine.dispose() call in
              app/database.py?&quot; or &quot;what calls
              AuthService.login?&quot; The guardian uses its memory to
              produce a structured assessment with severity, specific
              concerns, and confidence.
            </p>
          </div>
          <div>
            <h3 className="font-medium">Iteration context</h3>
            <p className="text-sm text-muted-foreground mt-1">
              Every time you iterate, the Builder receives the
              guardian&apos;s summaries of files near the changes
              you&apos;re making. This is why the AI knows about other
              files it didn&apos;t directly edit — the memory layer
              keeps the project&apos;s structure in scope without
              re-reading every file.
            </p>
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <Shield className="h-5 w-5 text-amber-400" />
          What languages it understands
        </h2>
        <p className="leading-relaxed">
          File-level summaries work for any text-based source file. The
          guardian recognizes specific concerns for:
        </p>
        <div className="grid grid-cols-2 gap-2 text-sm">
          <div>Python, TypeScript, JavaScript</div>
          <div>Go, Rust, Java</div>
          <div>C, C++, Ruby, PHP</div>
          <div>PowerShell, KQL, bash, AppleScript</div>
        </div>
        <p className="leading-relaxed">
          Symbol-level extraction (function/class/method-grained
          memory) works for the first 12 languages above via tree-
          sitter parsers. PowerShell, KQL, bash, and AppleScript get
          deep file-level audit but not symbol-grained indexing yet.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">
          Why indexing takes a few minutes
        </h2>
        <p className="leading-relaxed">
          The guardian runs on a local model (Qwen 2.5 Coder) rather
          than a frontier API. This keeps your code on infrastructure
          we control and costs nothing per call — but it means
          indexing happens on CPU, sequentially, at roughly 10 seconds
          per file plus 10 seconds per function. A typical 17-file
          project with ~60 symbols takes about 12 minutes of background
          indexing.
        </p>
        <p className="leading-relaxed">
          File-level summaries land first — usually within the first
          90 seconds. Symbol-level summaries arrive in waves over the
          next several minutes. You can iterate normally during this
          window; the AI gets richer context as more memory accumulates.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">
          How to spot stale memory
        </h2>
        <p className="leading-relaxed">
          If a file shows a <code className="px-1 rounded bg-orange-950/40 text-orange-300 text-xs">stale</code> pill
          in the Project Memory panel, the file was modified after the
          summary was produced and the summary may be out of date. Click
          the small refresh icon next to that file&apos;s name to
          re-index just that file. Otherwise the next iteration auto-
          re-indexes the whole project.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">What it doesn&apos;t do</h2>
        <ul className="space-y-2 ml-4 list-disc list-inside">
          <li>
            The guardian doesn&apos;t run your code or execute tests.
            It reads and reasons about source.
          </li>
          <li>
            It doesn&apos;t cross your project&apos;s boundaries.
            Imports referencing third-party packages don&apos;t get
            indexed — only files inside your project.
          </li>
          <li>
            It doesn&apos;t persist data outside Code Flow. Summaries
            live in the same database as your project and are deleted
            when you delete the project.
          </li>
        </ul>
      </section>

      <div className="border-t border-border pt-6 mt-12 text-sm text-muted-foreground">
        <p>
          Have a question this page doesn&apos;t cover?{" "}
          <Link href="/" className="text-violet-400 hover:underline">
            Open a project
          </Link>{" "}
          and ask the risk analyzer about something specific.
        </p>
      </div>
    </div>
  );
}
