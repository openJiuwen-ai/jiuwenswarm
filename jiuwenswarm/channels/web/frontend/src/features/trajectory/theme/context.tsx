// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { createContext, type ReactNode, useContext } from 'react'

/** Theme variants retained by the migrated trajectory presentation layer. */
export type TrajectoryColorMode = 'light' | 'dark'

const TrajectoryColorModeContext = createContext<TrajectoryColorMode>('light')

/** Keep theme metadata available to content rendered through a React portal. */
export function TrajectoryThemeProvider({
  children,
  colorMode,
}: {
  children: ReactNode
  colorMode: TrajectoryColorMode
}) {
  return (
    <TrajectoryColorModeContext.Provider value={colorMode}>
      {children}
    </TrajectoryColorModeContext.Provider>
  )
}

/** Read the trajectory-local color mode without coupling to the host theme store. */
export function useTrajectoryColorMode(): TrajectoryColorMode {
  return useContext(TrajectoryColorModeContext)
}
