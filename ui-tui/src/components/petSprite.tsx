import { Box, Text } from '@hermes/ink'
import { memo } from 'react'

// A cell is [tr,tg,tb,ta, br,bg,bb,ba] — the top + bottom pixel of one
// half-block, as produced by the `pet.cells` gateway RPC.
export type PetCell = number[]
export type PetGrid = PetCell[][]

const HALF_BLOCK = '▀'

const hex = (r: number, g: number, b: number) =>
  `#${[r, g, b].map(v => Math.max(0, Math.min(255, v | 0)).toString(16).padStart(2, '0')).join('')}`

/**
 * Renders one petdex frame as truecolor half-blocks using native Ink color
 * props (no raw ANSI, so width measurement stays correct). The engine
 * (`agent/pet/render.py`) does the decode + downscale; this is a thin painter.
 */
export const PetSprite = memo(function PetSprite({ grid }: { grid: PetGrid }) {
  if (!grid.length) {
    return null
  }

  return (
    <Box flexDirection="column">
      {grid.map((row, y) => (
        <Box key={y}>
          {row.map((cell, x) => {
            const [tr, tg, tb, ta, br, bg, bb, ba] = cell
            if ((ta ?? 0) < 32 && (ba ?? 0) < 32) {
              return <Text key={x}> </Text>
            }
            return (
              <Text backgroundColor={hex(br, bg, bb)} color={hex(tr, tg, tb)} key={x}>
                {HALF_BLOCK}
              </Text>
            )
          })}
        </Box>
      ))}
    </Box>
  )
})
