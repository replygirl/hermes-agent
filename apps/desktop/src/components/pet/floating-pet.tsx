import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { persistString, storedString } from '@/lib/storage'
import { $petInfo, type PetInfo, setPetInfo } from '@/store/pet'
import { $gatewayState } from '@/store/session'

import { PetSprite } from './pet-sprite'

// v2: positions are now top/left anchored (v1 stored bottom-anchored values,
// which dragged inverted). Bumping the key discards stale v1 coordinates.
const POSITION_KEY = 'hermes.desktop.pet-position.v2'

interface Point {
  x: number
  y: number
}

function clampToViewport({ x, y }: Point): Point {
  const maxX = Math.max(0, (window.innerWidth || 800) - 80)
  const maxY = Math.max(0, (window.innerHeight || 600) - 80)

  return { x: Math.min(Math.max(0, x), maxX), y: Math.min(Math.max(0, y), maxY) }
}

function loadPosition(): Point {
  try {
    const raw = storedString(POSITION_KEY)

    if (raw) {
      const parsed = JSON.parse(raw) as Point

      if (typeof parsed.x === 'number' && typeof parsed.y === 'number') {
        return clampToViewport(parsed)
      }
    }
  } catch {
    // fall through to default
  }

  // Default: lower-left corner (top/left anchored).
  return clampToViewport({ x: 24, y: (window.innerHeight || 600) - 220 })
}

/**
 * In-window floating petdex mascot. Always-on-top within the app, draggable,
 * and reactive to agent activity via `$petState`. Fetches the active pet via
 * the shared `pet.info` RPC; renders nothing until a pet is installed +
 * enabled.
 *
 * Adopting a pet is fully in-app: type `/pet boba` in the composer. That
 * writes `display.pet.*` from the slash worker, so we keep polling `pet.info`
 * while no pet is active and the mascot pops in within a few seconds — no
 * reload, no CLI. Once a pet is live we stop polling.
 *
 * Promotion to a separate frameless OS-level window is a follow-up — the
 * sprite + state logic here is reused as-is, only the host changes.
 */
const PET_POLL_MS = 3000

export function FloatingPet() {
  const { requestGateway } = useGatewayRequest()
  const gatewayState = useStore($gatewayState)
  const info = useStore($petInfo)

  const [position, setPosition] = useState<Point>(loadPosition)
  const containerRef = useRef<HTMLDivElement | null>(null)
  // Live drag offset (pointer → element top-left). Drag updates the DOM
  // directly to avoid a React re-render (and canvas reflow) per pointermove —
  // state is only committed on release.
  const dragRef = useRef<{ dx: number; dy: number; x: number; y: number } | null>(null)

  // Fetch pet.info on connect, then keep polling while no pet is active so an
  // in-app `/pet <slug>` shows up live. Stops polling once a pet is enabled.
  const active = info.enabled && Boolean(info.spritesheetBase64)
  useEffect(() => {
    if (gatewayState !== 'open' || active) {
      return
    }

    let cancelled = false

    const pull = async () => {
      try {
        const next = await requestGateway<PetInfo>('pet.info')

        if (!cancelled && next) {
          setPetInfo(next)
        }
      } catch {
        // cosmetic feature — never surface gateway errors
      }
    }

    void pull()
    const timer = window.setInterval(() => void pull(), PET_POLL_MS)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [gatewayState, active, requestGateway])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const el = containerRef.current

    if (!el) {
      return
    }

    const rect = el.getBoundingClientRect()
    dragRef.current = { dx: e.clientX - rect.left, dy: e.clientY - rect.top, x: rect.left, y: rect.top }
    el.setPointerCapture(e.pointerId)
    el.style.cursor = 'grabbing'
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const drag = dragRef.current
    const el = containerRef.current

    if (!drag || !el) {
      return
    }

    const next = clampToViewport({ x: e.clientX - drag.dx, y: e.clientY - drag.dy })
    drag.x = next.x
    drag.y = next.y
    // Mutate the DOM directly — no setState, so no re-render while dragging.
    el.style.left = `${next.x}px`
    el.style.top = `${next.y}px`
  }, [])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    const drag = dragRef.current

    if (drag) {
      dragRef.current = null
      const committed = { x: drag.x, y: drag.y }
      setPosition(committed)
      persistString(POSITION_KEY, JSON.stringify(committed))
    }

    const el = containerRef.current

    if (el) {
      el.style.cursor = 'grab'
      el.releasePointerCapture?.(e.pointerId)
    }
  }, [])

  if (!info.enabled || !info.spritesheetBase64) {
    return null
  }

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      ref={containerRef}
      style={{
        cursor: 'grab',
        left: position.x,
        pointerEvents: 'auto',
        position: 'fixed',
        top: position.y,
        touchAction: 'none',
        userSelect: 'none',
        zIndex: 60
      }}
      title={info.displayName || 'pet'}
    >
      <PetSprite info={info} />
    </div>
  )
}
