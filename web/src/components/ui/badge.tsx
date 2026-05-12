import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/80",
        outline: "text-foreground",
        // Custom severity variants — used by FindingList. These don't
        // follow shadcn's stock set exactly because we want distinct
        // colors for critical / warning / nit that read well in dark mode.
        critical:
          "border-transparent bg-red-900/60 text-red-200 hover:bg-red-900/80",
        warning:
          "border-transparent bg-amber-900/50 text-amber-200 hover:bg-amber-900/70",
        nit:
          "border-transparent bg-zinc-700 text-zinc-300 hover:bg-zinc-600",
        // For provider tags (anthropic/openai/google).
        provider:
          "border-transparent bg-zinc-800 text-zinc-300",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
