import { useEffect, useRef, useState } from 'react'

import type { PetGrid } from '../components/petSprite.js'

import { useGateway } from './gatewayContext.js'
import { $turnState } from './turnStore.js'
import { $uiState } from './uiStore.js'

export type PetState = 'idle' | 'wave' | 'run' | 'failed' | 'review' | 'jump'

interface PetActivity {
  busy: boolean
  toolRunning: boolean
  reasoning: boolean
}

/**
 * Resolve the animation state — mirrors `agent.pet.state.derive_pet_state`
 * (and the desktop's `derivePetState`) so all surfaces agree.
 */
export function derivePetState({ busy, toolRunning, reasoning }: PetActivity): PetState {
  if (toolRunning) {
    return 'run'
  }

  if (reasoning) {
    return 'review'
  }

  if (busy) {
    return 'run'
  }

  return 'idle'
}

interface PetCellsResult {
  enabled?: boolean
  frameMs?: number
  frames?: PetGrid[]
  state?: string
}

/**
 * Drives the TUI pet: derives the live state from the turn/ui stores, lazily
 * fetches each state's half-block frames via the `pet.cells` RPC (cached),
 * and animates the frame index. Returns the grid to paint, or null when no
 * pet is enabled/installed.
 */
export function usePet(): { enabled: boolean; grid: PetGrid | null } {
  const { rpc } = useGateway()
  const [enabled, setEnabled] = useState(false)
  const [grid, setGrid] = useState<PetGrid | null>(null)

  const cache = useRef<Map<PetState, { frameMs: number; frames: PetGrid[] }>>(new Map())
  const stateRef = useRef<PetState>('idle')
  const frameRef = useRef(0)
  const probed = useRef(false)

  // Recompute the desired state on every turn/ui change.
  const [petState, setPetState] = useState<PetState>('idle')
  useEffect(() => {
    const recompute = () => {
      const turn = $turnState.get()
      const ui = $uiState.get()

      const next = derivePetState({
        busy: ui.busy,
        toolRunning: turn.tools.length > 0,
        reasoning: turn.reasoningActive
      })

      stateRef.current = next
      setPetState(next)
    }

    recompute()
    const unsubTurn = $turnState.listen(recompute)
    const unsubUi = $uiState.listen(recompute)

    return () => {
      unsubTurn()
      unsubUi()
    }
  }, [])

  // Fetch frames for the current state (lazily, cached).
  useEffect(() => {
    let cancelled = false

    if (cache.current.has(petState)) {
      frameRef.current = 0

      return
    }
    void (async () => {
      try {
        const res = (await rpc('pet.cells', { state: petState })) as PetCellsResult | null

        if (cancelled || !res) {
          return
        }

        if (!probed.current) {
          probed.current = true
          setEnabled(Boolean(res.enabled))
        }

        if (res.enabled && res.frames?.length) {
          cache.current.set(petState, { frameMs: res.frameMs ?? 180, frames: res.frames })
          frameRef.current = 0
        }
      } catch {
        // cosmetic — ignore RPC failures
      }
    })()

    return () => {
      cancelled = true
    }
  }, [petState, rpc])

  // While no pet is active, poll `pet.cells` so an in-app `/pet <slug>` (which
  // writes display.pet.* from the slash worker) lights the pet up live — no
  // restart. Stops once a pet is enabled.
  useEffect(() => {
    if (enabled) {
      return
    }

    let cancelled = false

    const probe = async () => {
      try {
        const res = (await rpc('pet.cells', { state: stateRef.current })) as PetCellsResult | null

        if (cancelled || !res?.enabled || !res.frames?.length) {
          return
        }

        cache.current.set(stateRef.current, { frameMs: res.frameMs ?? 180, frames: res.frames })
        frameRef.current = 0
        setEnabled(true)
      } catch {
        // cosmetic — ignore RPC failures
      }
    }

    const timer = setInterval(() => void probe(), 3000)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [enabled, rpc])

  // Animation timer.
  useEffect(() => {
    if (!enabled) {
      return
    }

    const tick = () => {
      const entry = cache.current.get(stateRef.current)

      if (!entry || !entry.frames.length) {
        setGrid(null)

        return
      }

      const idx = frameRef.current % entry.frames.length
      setGrid(entry.frames[idx] ?? null)
      frameRef.current = idx + 1
    }

    tick()
    const interval = setInterval(tick, 160)

    return () => clearInterval(interval)
  }, [enabled, petState])

  return { enabled, grid }
}
