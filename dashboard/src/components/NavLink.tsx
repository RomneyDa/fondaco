"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

interface NavLinkProps {
  href: string;
  className?: string | ((props: { isActive: boolean }) => string);
  children: React.ReactNode;
}

export function NavLink({ href, className, children }: NavLinkProps) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname.startsWith(href + "/");

  const cls = typeof className === "function" ? className({ isActive }) : className;

  return (
    <Link href={href} className={cls}>
      {children}
    </Link>
  );
}
