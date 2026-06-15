import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { triggerHaptic } from '@/lib/haptics'
import { Loader2, PawPrint, Trash2 } from '@/lib/icons'
import { selectableCardClass } from '@/lib/selectable-card'
import { cn } from '@/lib/utils'
import { type PetInfo, setPetInfo } from '@/store/pet'
import { $gatewayState } from '@/store/session'

import { ListRow, SectionHeading } from './primitives'

/** A JSON-RPC "method not found" — the backend predates the pet RPCs. */
function isMissingMethod(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /method not found|-32601|unknown method|no such method/i.test(message)
}

interface GalleryPet {
  slug: string
  displayName: string
  installed: boolean
  spritesheetUrl?: string
  /** petdex's hand-picked/official set — the closest thing to "popular." */
  curated?: boolean
}

// petdex frames are a fixed 192×208 grid; the box matches that aspect.
const THUMB_W = 40
const THUMB_H = Math.round((THUMB_W * 208) / 192)

type ThumbLoader = (slug: string, url?: string) => Promise<string | null>

/**
 * Idle-frame preview for one pet. The backend crops + caches the frame and
 * returns it as a same-origin data URI (`pet.thumb`), which dodges the renderer
 * CSP / R2 hotlink rules that break a direct `<img src=cdn>`. We only fire the
 * request once the thumb scrolls into view, so the picker never fetches the
 * whole catalog up front.
 */
function PetThumb({ slug, url, alt, load }: { slug: string; url?: string; alt: string; load: ThumbLoader }) {
  const [src, setSrc] = useState<string | null>(null)
  const boxRef = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    const el = boxRef.current

    if (!el || src) {
      return
    }

    const observer = new IntersectionObserver(
      entries => {
        if (entries.some(entry => entry.isIntersecting)) {
          observer.disconnect()
          void load(slug, url).then(uri => {
            if (uri) {
              setSrc(uri)
            }
          })
        }
      },
      { rootMargin: '120px' }
    )

    observer.observe(el)

    return () => observer.disconnect()
  }, [slug, url, src, load])

  return (
    <span
      className="grid shrink-0 place-items-center overflow-hidden rounded-md bg-(--ui-bg-tertiary) text-(--ui-text-tertiary)"
      ref={boxRef}
      style={{ height: THUMB_H, width: THUMB_W }}
    >
      {src ? (
        <img
          alt={alt}
          aria-hidden
          className="pointer-events-none size-full object-contain"
          src={src}
          style={{ imageRendering: 'pixelated' }}
        />
      ) : (
        <PawPrint className="size-4" />
      )}
    </span>
  )
}

interface PetGallery {
  enabled: boolean
  active: string
  pets: GalleryPet[]
}

/**
 * Appearance opt-in for the floating petdex mascot. Reads the gallery + current
 * config via `pet.gallery`, adopts a pet with `pet.select` (installs on demand),
 * and toggles off with `pet.disable`. The floating mascot polls `pet.info`, so
 * picking a pet here lights it up within a couple seconds — no reload, no CLI.
 */
export function PetSettings() {
  const { requestGateway } = useGatewayRequest()
  const gatewayState = useStore($gatewayState)
  const [gallery, setGallery] = useState<PetGallery | null>(null)
  const [busySlug, setBusySlug] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [staleBackend, setStaleBackend] = useState(false)
  const [query, setQuery] = useState('')

  // Dedupe thumb requests per slug (across re-renders and re-filters); the
  // backend also disk-caches, so a slug is fetched at most once per session.
  const thumbCache = useRef<Map<string, Promise<string | null>>>(new Map())

  const loadThumb = useCallback<ThumbLoader>(
    (slug, url) => {
      const cache = thumbCache.current
      let pending = cache.get(slug)

      if (!pending) {
        pending = requestGateway<{ ok: boolean; dataUri?: string }>('pet.thumb', { slug, url: url ?? '' })
          .then(result => (result?.ok && result.dataUri ? result.dataUri : null))
          .catch(() => null)
        cache.set(slug, pending)
      }

      return pending
    },
    [requestGateway]
  )

  const RESTART_HINT =
    'Pets need a quick restart — the running app started before this feature was added. Quit and reopen Hermes, then come back here.'

  const refresh = useCallback(async () => {
    try {
      // Pull the picker state AND push the live mascot state into the shared
      // `$petInfo` store, so the floating pet reflects a change/disable here
      // immediately instead of clinging to its cached sprite.
      const [next, info] = await Promise.all([
        requestGateway<PetGallery>('pet.gallery'),
        requestGateway<PetInfo>('pet.info')
      ])

      if (next) {
        setGallery(next)
        setStaleBackend(false)
      }

      if (info) {
        setPetInfo(info)
      }
    } catch (e) {
      if (isMissingMethod(e)) {
        setStaleBackend(true)
      }
      // otherwise cosmetic — leave the picker as-is on a transient hiccup
    }
  }, [requestGateway])

  useEffect(() => {
    if (gatewayState !== 'open') {
      return
    }

    void refresh()
  }, [gatewayState, refresh])

  const enabled = gallery?.enabled ?? false
  const active = gallery?.active ?? ''
  const pets = gallery?.pets ?? []

  // Every mutation shares the same shape: spin the row, fire the RPC, resync.
  // A missing method means a stale backend; anything else is a real error.
  const runPetRpc = useCallback(
    async (method: string, slug: string, failMsg: string) => {
      setBusySlug(slug)
      setError(null)

      try {
        await requestGateway(method, slug ? { slug } : undefined)
        triggerHaptic('crisp')
        await refresh()
      } catch (e) {
        if (isMissingMethod(e)) {
          setStaleBackend(true)
        } else {
          setError(e instanceof Error ? e.message : failMsg)
        }
      } finally {
        setBusySlug(null)
      }
    },
    [refresh, requestGateway]
  )

  const selectPet = useCallback((slug: string) => runPetRpc('pet.select', slug, `Could not adopt ${slug}`), [runPetRpc])

  const removePet = useCallback(
    (slug: string) => runPetRpc('pet.remove', slug, `Could not uninstall ${slug}`),
    [runPetRpc]
  )

  const toggle = useCallback(
    (on: boolean) => {
      if (!on) {
        return runPetRpc('pet.disable', '', 'Could not turn the pet off.')
      }

      const slug = gallery?.active || gallery?.pets[0]?.slug

      if (!slug) {
        setError('No pets available to turn on right now.')

        return
      }

      return selectPet(slug)
    },
    [gallery, runPetRpc, selectPet]
  )

  // Installed pets first, then the rest of the gallery. The petdex catalog is
  // thousands of entries, so filter by query and cap how many we render.
  const RENDER_CAP = 60
  const needle = query.trim().toLowerCase()

  const filtered = pets.filter(
    pet =>
      !/^clawd(-|$)/i.test(pet.slug) &&
      (!needle || pet.slug.toLowerCase().includes(needle) || pet.displayName.toLowerCase().includes(needle))
  )

  // petdex has no popularity data, so rank by the signals we do have: the
  // active pet first, then installed, then curated (official), then the rest.
  const rank = (pet: GalleryPet) =>
    Number(enabled && pet.slug === active) * 4 + Number(pet.installed) * 2 + Number(pet.curated)

  const sorted = [...filtered].sort((a, b) => rank(b) - rank(a))
  const shown = sorted.slice(0, RENDER_CAP)

  return (
    <div>
      <SectionHeading icon={PawPrint} title="Pet" />
      <p className="max-w-2xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        Adopt an animated petdex mascot that floats over the app and reacts to what Hermes is doing — running while
        tools execute, celebrating on success, sulking on errors.
      </p>

      {staleBackend && (
        <p className="mt-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-2 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
          {RESTART_HINT}
        </p>
      )}

      <div className="mt-2">
        <ListRow
          action={
            <SegmentedControl
              onChange={id => void toggle(id === 'on')}
              options={[
                { id: 'off', label: 'Off' },
                { id: 'on', label: 'On' }
              ]}
              value={enabled ? 'on' : 'off'}
            />
          }
          description={
            enabled && active ? `Showing ${active}.` : 'Turn on to show your mascot in the corner of the window.'
          }
          title="Floating mascot"
        />

        <ListRow
          below={
            <>
              <input
                className="mt-3 w-full rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-1.5 text-[length:var(--conversation-caption-font-size)] outline-none placeholder:text-(--ui-text-tertiary) focus:border-(--ui-stroke-secondary)"
                onChange={event => setQuery(event.target.value)}
                placeholder="Search pets…"
                spellCheck={false}
                value={query}
              />
              {/* Fixed-height scroll area so filtering never grows/shrinks the
                  page (no layout thrash); the grid scrolls inside it. */}
              <div className="mt-3 h-72 overflow-y-auto pr-1">
                {pets.length === 0 ? (
                  <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                    Couldn't reach the petdex gallery. Check your connection and reopen this page.
                  </p>
                ) : shown.length === 0 ? (
                  <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                    No pets match "{query}".
                  </p>
                ) : (
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {shown.map(pet => {
                      const isActive = enabled && active === pet.slug
                      const isBusy = busySlug === pet.slug

                      return (
                        <div className="group relative" key={pet.slug}>
                          <button
                            className={cn(
                              'flex w-full items-center gap-2.5 px-2.5 py-2 text-left disabled:opacity-50',
                              selectableCardClass({ active: isActive, prominent: pet.installed })
                            )}
                            disabled={isBusy}
                            onClick={() => void selectPet(pet.slug)}
                            type="button"
                          >
                            <PetThumb alt={pet.displayName} load={loadThumb} slug={pet.slug} url={pet.spritesheetUrl} />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[length:var(--conversation-text-font-size)] font-medium">
                                {pet.displayName}
                              </span>
                              <span className="block truncate text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                                {pet.slug}
                                {pet.installed ? ' · installed' : pet.curated ? ' · official' : ''}
                              </span>
                            </span>
                            {isBusy && <Loader2 className="size-4 shrink-0 animate-spin text-(--ui-text-tertiary)" />}
                          </button>
                          {pet.installed && !isBusy && (
                            <button
                              aria-label={`Uninstall ${pet.displayName}`}
                              className="absolute right-1.5 top-1.5 grid size-6 place-items-center rounded-md bg-(--ui-bg-elevated)/80 text-(--ui-text-tertiary) opacity-0 backdrop-blur-sm transition hover:text-(--ui-red) focus-visible:opacity-100 group-hover:opacity-100"
                              onClick={() => void removePet(pet.slug)}
                              title={`Uninstall ${pet.displayName}`}
                              type="button"
                            >
                              <Trash2 className="size-3.5" />
                            </button>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
              {/* Always-present status line so its appearance never shifts layout. */}
              <p className="mt-2 min-h-4 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                {error ? (
                  <span className="text-(--ui-red)">{error}</span>
                ) : sorted.length > RENDER_CAP ? (
                  `Showing ${RENDER_CAP} of ${sorted.length} — type to narrow it down.`
                ) : (
                  `${sorted.length} pet${sorted.length === 1 ? '' : 's'}.`
                )}
              </p>
            </>
          }
          description="Picking one installs it (if needed) and makes it active."
          title="Choose a pet"
          wide
        />
      </div>
    </div>
  )
}
