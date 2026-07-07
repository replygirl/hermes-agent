import { describe, expect, it } from "vitest";
import { api, authedFetch, buildWsAuthParam, fetchJSON, getManagementProfile, hermesDashboardConsole, setManagementProfile } from "./api";

describe("hermesDashboardConsole", () => {
  it("exposes the authenticated dashboard API surface for DevTools automation", () => {
    expect(hermesDashboardConsole.api).toBe(api);
    expect(hermesDashboardConsole.fetchJSON).toBe(fetchJSON);
    expect(hermesDashboardConsole.authedFetch).toBe(authedFetch);
    expect(hermesDashboardConsole.buildWsAuthParam).toBe(buildWsAuthParam);
    expect(hermesDashboardConsole.getManagementProfile).toBe(getManagementProfile);
    expect(hermesDashboardConsole.setManagementProfile).toBe(setManagementProfile);
    expect(typeof hermesDashboardConsole.api.getSessions).toBe("function");
    expect(hermesDashboardConsole.basePath).toBe("");
  });
});
