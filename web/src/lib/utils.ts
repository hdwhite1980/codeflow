import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge Tailwind classes with proper conflict resolution.
 *
 * Standard shadcn helper. `clsx` handles conditional/falsy values,
 * `tailwind-merge` resolves conflicts (e.g. "px-2 px-4" → "px-4").
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
