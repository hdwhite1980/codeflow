"use client";

import { useState } from "react";
import { Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { iterateProject } from "@/lib/api";

/**
 * Chat-style prompt input pinned to the bottom of the graph view.
 *
 * Workflow
 * --------
 *   1. User types a follow-up prompt ("add a dark mode toggle").
 *   2. Submit → POST /api/projects/<id>/iterate → returns immediately
 *      with the assigned iteration_seq.
 *   3. The parent component starts tracking that iteration_seq as
 *      "in flight" and shows progress in the graph (optimistic
 *      writing-status updates) and in the history sidebar.
 *
 * Validation
 * ----------
 * Backend requires 5-2000 chars. We mirror the bounds here so the user
 * gets immediate feedback on a too-short or too-long prompt.
 *
 * Concurrency
 * -----------
 * We don't prevent the user from queueing multiple iterations
 * back-to-back. The backend will process them serially through the
 * job queue. The textarea clears after submit so the user can type
 * the next one immediately if they want.
 */
interface IterationInputProps {
  projectId: string;
  /**
   * Called immediately after a successful iterate POST, with the new
   * iteration's seq number. The parent uses this to show "iteration N
   * is running" state and seed optimistic graph mutations.
   */
  onIterationQueued: (seq: number, prompt: string) => void;
  /**
   * Whether ANY iteration is currently in flight. We dim the input
   * slightly when one is running but DON'T disable it — the user
   * can queue follow-ups. The disabled state is reserved for the
   * brief moment we're submitting the form.
   */
  someIterationRunning: boolean;
}

export function IterationInput({
  projectId,
  onIterationQueued,
  someIterationRunning,
}: IterationInputProps) {
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function validate(): string | null {
    const trimmed = prompt.trim();
    if (trimmed.length < 5) return "Prompt must be at least 5 characters.";
    if (trimmed.length > 2000) return "Prompt must be 2000 characters or fewer.";
    return null;
  }

  async function submit() {
    setError(null);
    const v = validate();
    if (v) {
      setError(v);
      return;
    }
    const promptToSend = prompt.trim();
    setSubmitting(true);
    try {
      const resp = await iterateProject(projectId, { prompt: promptToSend });
      onIterationQueued(resp.iteration_seq, promptToSend);
      setPrompt(""); // Clear so the user can immediately type another.
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to queue iteration. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  function onKeyDown(ev: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Cmd/Ctrl + Enter submits — the standard for chat-style inputs.
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault();
      void submit();
    }
  }

  return (
    <div
      className={`pointer-events-auto rounded-lg border bg-card p-3 shadow-lg ${
        someIterationRunning ? "border-blue-700/40" : "border-border"
      }`}
    >
      <div className="mb-2 flex items-center justify-between">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Iterate on this build
        </label>
        {someIterationRunning && (
          <span className="text-[10px] uppercase tracking-wide text-blue-400">
            iteration running…
          </span>
        )}
      </div>
      <Textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Describe a change… e.g. 'add a /version endpoint', 'switch DB to MySQL', 'add tests for the converter'"
        rows={2}
        disabled={submitting}
        className="resize-none text-sm"
      />
      {error && (
        <div className="mt-2 rounded-md border border-red-900/40 bg-red-950/30 px-2 py-1 text-xs text-red-300">
          {error}
        </div>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[10px] text-muted-foreground">
          ⌘/Ctrl + Enter to submit
        </span>
        <Button
          size="sm"
          onClick={() => void submit()}
          disabled={submitting || prompt.trim().length < 5}
        >
          <Send className="mr-1 h-3 w-3" />
          {submitting ? "Queuing…" : "Iterate"}
        </Button>
      </div>
    </div>
  );
}
